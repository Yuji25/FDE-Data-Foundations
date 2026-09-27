"""Reproducible FlashEats run-date pipeline; no stakeholder KPI approval implied."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
import json
import logging
import math
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import pandas as pd
import requests

from . import extract
from .clean import clean_sources
from .config import PipelineConfig
from .logging_utils import setup_logger
from .metrics import calculate_metrics
from .transform import build_order_journey
from .validate import validate_sources


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_ROOT = PROJECT_ROOT / "data" / "processed"
PUBLISHED_FILES = ("order_journey.jsonl", "quality_report.json", "cleaning_report.json",
                   "metrics.json", "run_manifest.json")


class PipelineRunError(RuntimeError):
    """A run failed without publishing an incomplete analytical partition."""


def _run_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--run-date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("--run-date must be YYYY-MM-DD")
    return value


@contextmanager
def _managed_mock(api_url: str, enabled: bool):
    """Start only on an unused local port; terminate our own child on every exit."""
    if not enabled:
        yield
        return
    parsed = urlsplit(api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} \
            or parsed.port is None or parsed.path not in {"", "/"}:
        raise PipelineRunError("--start-mock requires a plain local http://host:port DISPATCH_API_URL")
    host, port = parsed.hostname, parsed.port
    with socket.socket() as probe:
        # Permit a prior managed server's TIME_WAIT sockets, but not a live listener.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise PipelineRunError(f"Mock port {host}:{port} is occupied; refusing to use or stop that service") from exc
    process = subprocess.Popen([sys.executable, "-m", "mock_api.server", "--host", host,
                                "--port", str(port)], cwd=PROJECT_ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise PipelineRunError(f"Mock API exited during startup with code {process.returncode}")
            try:
                response = requests.get(api_url.rstrip("/") + "/health", timeout=0.5)
                body = response.json()
                if response.status_code == 200 and body == {
                    "status": "ok", "service": "flasheats-dispatch-api"}:
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.1)
        else:
            raise PipelineRunError("Mock API did not become healthy within 10 seconds")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()


def _json_safe(value):
    """Preserve object-valued model fields without inventing a timezone."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError, AttributeError):
            pass
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    raise TypeError(f"Unsupported model value for JSON: {type(value).__name__}")


def _write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(_json_safe(payload), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _write_model(path: Path, model: pd.DataFrame) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in model.to_dict(orient="records"):
            handle.write(json.dumps(_json_safe(row), sort_keys=True, allow_nan=False) + "\n")


def _publish_evidence(processed_root: Path, run_date: str, model: pd.DataFrame,
                      quality: dict, cleaning: dict, metrics: dict, manifest: dict) -> Path:
    processed_root.mkdir(parents=True, exist_ok=True)
    destination = processed_root / f"run_date={run_date}"
    staging = Path(tempfile.mkdtemp(prefix=f".run_date={run_date}-", dir=processed_root))
    try:
        _write_model(staging / "order_journey.jsonl", model)
        _write_json(staging / "quality_report.json", quality)
        _write_json(staging / "cleaning_report.json", cleaning)
        _write_json(staging / "metrics.json", metrics)
        _write_json(staging / "run_manifest.json", manifest)
        # Check staged evidence before the atomic directory replacement.
        for name in PUBLISHED_FILES:
            with (staging / name).open(encoding="utf-8") as handle:
                if name.endswith(".jsonl"):
                    records = [json.loads(line) for line in handle]
                    if len(records) != len(model) or len({row["order_id"] for row in records}) != len(model):
                        raise PipelineRunError("Staged order model lost rows or uniqueness")
                else:
                    json.load(handle)
        extract._publish_snapshot(staging, destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _failure_evidence(processed_root: Path, run_date: str, quality: dict,
                      cleaning: dict | None, reason: str) -> Path:
    root = processed_root / "diagnostics" / f"run_date={run_date}"
    root.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="attempt-", dir=root))
    _write_json(destination / "quality_report.json", quality)
    if cleaning is not None:
        _write_json(destination / "cleaning_report.json", cleaning)
    _write_json(destination / "failure_manifest.json", {"run_date": run_date, "reason": reason,
                "gate": quality.get("gate"), "published_model": False})
    return destination


def run_pipeline(run_date: str, config: PipelineConfig, logger: logging.Logger,
                 *, start_mock: bool = False, raw_data_dir: Path = RAW_DATA_DIR,
                 processed_root: Path = PROCESSED_ROOT) -> dict:
    """Execute existing stages and atomically publish only a permitted model."""
    run_date = _run_date(run_date)
    processed_root = Path(processed_root)
    with _managed_mock(config.dispatch_api_url, start_mock):
        sources = extract.extract_local_sources(raw_data_dir, logger)
        dispatch = extract.extract_dispatch_api(config.dispatch_api_url, run_date, config, logger)
        _, quality, allowed = validate_sources(sources, dispatch)
        if not allowed:
            diagnostic = _failure_evidence(processed_root, run_date, quality, None,
                                           "Fatal source validation rule")
            raise PipelineRunError(f"Quality gate FAIL; diagnostics: {diagnostic}")
        cleaned, cleaning = clean_sources(sources, dispatch)
        if not cleaning["can_continue"]:
            diagnostic = _failure_evidence(processed_root, run_date, cleaning.get("quality", quality), cleaning,
                                           "Fatal cleaning quality gate")
            raise PipelineRunError(f"Cleaning gate FAIL; diagnostics: {diagnostic}")
        model = build_order_journey(cleaned, cleaning)
        metrics = calculate_metrics(model)
        snapshot = Path(extract.DISPATCH_RAW_ROOT) / f"run_date={run_date}"
        page_files = sorted(snapshot.glob("page_*.json"))
        source_counts = {name: len(value) for name, value in sources.items()
                         if isinstance(value, pd.DataFrame)}
        source_counts["dispatch"] = len(dispatch)
        source_counts["metadata_documents"] = len(sources.get("metadata", {}))
        manifest = {"run_date": run_date, "quality_gate": quality["gate"],
                    "model_rows": len(model), "unique_order_ids": int(model.order_id.nunique()),
                    "source_rows": source_counts, "dispatch_page_count": len(page_files),
                    "dispatch_record_count": len(dispatch),
                    "dispatch_raw_partition": str(snapshot.relative_to(PROJECT_ROOT))
                    if snapshot.is_relative_to(PROJECT_ROOT) else str(snapshot),
                    "published_files": list(PUBLISHED_FILES), "model_format": "JSON Lines, one object per order",
                    "api_base_url": config.dispatch_api_url, "python_version": sys.version.split()[0]}
        destination = _publish_evidence(processed_root, run_date, model, quality, cleaning,
                                        metrics, manifest)
        logger.info("Published analytical evidence | run_date=%s gate=%s rows=%s path=%s",
                    run_date, quality["gate"], len(model), destination)
        return {"run_date": run_date, "gate": quality["gate"], "model_rows": len(model),
                "metrics": metrics, "output_dir": destination, "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the FlashEats Track A pipeline")
    parser.add_argument("--run-date", required=True, help="Logical YYYY-MM-DD partition date")
    parser.add_argument("--start-mock", action="store_true", help="Start and stop the submission-owned local API")
    args = parser.parse_args(argv)
    logger = None
    try:
        run_date = _run_date(args.run_date)
        config = PipelineConfig.from_env()
        logger = setup_logger("flasheats", config.log_level,
                              PROJECT_ROOT / "logs" / f"run_date={run_date}.log")
        result = run_pipeline(run_date, config, logger, start_mock=args.start_mock)
        ldr = result["metrics"]["candidate_operational_ldr"]
        support = result["metrics"]["support_lateness_variant"]
        print(f"Run date: {run_date} | gate: {result['gate']} | model rows: {result['model_rows']}")
        print(f"Candidate LDR: {ldr['numerator']}/{ldr['denominator']} = {ldr['rate']:.2%} (unapproved)")
        print(f">10-minute variant: {support['numerator']}/{support['denominator']} = {support['rate']:.2%}")
        print(f"Published: {result['output_dir']}")
        for name in PUBLISHED_FILES:
            print(f"  {result['output_dir'] / name}")
        return 0
    except Exception as exc:
        if logger is not None:
            logger.exception("FlashEats run failed")
        print(f"FlashEats run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
