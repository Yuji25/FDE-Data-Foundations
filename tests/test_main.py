"""End-to-end and atomic-publishing contracts for Phase 5."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import socket

import pandas as pd
import pytest

from pipeline import extract, main as pipeline_main
from pipeline.config import PipelineConfig


ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = "2026-09-27"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(port, retries=3):
    return PipelineConfig("INFO", retries, 1, f"http://127.0.0.1:{port}")


@pytest.fixture(scope="module")
def actual_data():
    sources = extract.extract_local_sources(ROOT / "data/raw", logging.getLogger("main-tests"))
    dispatch = pd.DataFrame(json.loads((ROOT / "mock_api/dispatch_data.json").read_text()))
    return sources, dispatch


@pytest.fixture(scope="module")
def successful_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase5")
    raw_dispatch = root / "dispatch"
    processed = root / "processed"
    port = _free_port()
    patcher = pytest.MonkeyPatch()
    patcher.setattr(extract, "DISPATCH_RAW_ROOT", raw_dispatch)
    try:
        result = pipeline_main.run_pipeline(RUN_DATE, _config(port), logging.getLogger("main-tests"),
                                            start_mock=True, processed_root=processed)
        yield result, raw_dispatch, processed, port
    finally:
        patcher.undo()


def test_actual_complete_run_and_valid_evidence(successful_run):
    result, raw_dispatch, _, _ = successful_run
    output = result["output_dir"]
    assert result["gate"] == "WARN" and result["model_rows"] == 1600
    assert {p.name for p in output.iterdir()} == set(pipeline_main.PUBLISHED_FILES)
    model = [json.loads(line) for line in (output / "order_journey.jsonl").read_text().splitlines()]
    assert len(model) == len({row["order_id"] for row in model}) == 1600
    assert sum(row["traffic_bucket"] is None for row in model) == 3
    assert isinstance(model[0]["order_source_rows"], list)
    assert isinstance(model[0]["order_event_type_counts"], dict)
    quality = json.loads((output / "quality_report.json").read_text())
    cleaning = json.loads((output / "cleaning_report.json").read_text())
    metrics = json.loads((output / "metrics.json").read_text())
    manifest = json.loads((output / "run_manifest.json").read_text())
    assert set(metrics) == {"population_orders", "candidate_operational_ldr",
                            "support_lateness_variant", "observed_lifecycle_durations",
                            "intervention_lateness_association", "traffic_lateness_segmentation"}
    assert set(manifest["published_files"]) == set(pipeline_main.PUBLISHED_FILES)
    assert manifest["source_rows"]["metadata_documents"] == 2
    assert quality["gate"] == cleaning["gate"] == "WARN"
    assert cleaning["sources"]["orders"]["unresolved_traffic_classifications"] == 3
    assert metrics["candidate_operational_ldr"]["numerator"] == 843
    assert metrics["candidate_operational_ldr"]["denominator"] == 1495
    assert metrics["support_lateness_variant"]["numerator"] == 349
    assert manifest["dispatch_page_count"] == 8
    assert manifest["dispatch_record_count"] == 1600
    assert len(list((raw_dispatch / f"run_date={RUN_DATE}").glob("page_*.json"))) == 8


def test_same_date_rerun_replaces_without_stale_files(successful_run):
    result, raw_dispatch, processed, port = successful_run
    output = result["output_dir"]
    before = {name: (output / name).read_bytes() for name in pipeline_main.PUBLISHED_FILES}
    (output / "stale.txt").write_text("must disappear")
    rerun = pipeline_main.run_pipeline(RUN_DATE, _config(port), logging.getLogger("rerun-tests"),
                                       start_mock=True, processed_root=processed)
    assert rerun["model_rows"] == 1600
    assert {p.name for p in output.iterdir()} == set(pipeline_main.PUBLISHED_FILES)
    assert {name: (output / name).read_bytes() for name in pipeline_main.PUBLISHED_FILES} == before
    assert len(list((raw_dispatch / f"run_date={RUN_DATE}").glob("page_*.json"))) == 8


def test_fail_gate_keeps_prior_partition_and_writes_diagnostics(successful_run, actual_data, monkeypatch):
    result, _, processed, _ = successful_run
    prior = (result["output_dir"] / "metrics.json").read_bytes()
    sources, dispatch = actual_data
    monkeypatch.setattr(pipeline_main.extract, "extract_local_sources", lambda *_: sources)
    monkeypatch.setattr(pipeline_main.extract, "extract_dispatch_api", lambda *_: dispatch)
    monkeypatch.setattr(pipeline_main, "validate_sources", lambda *_: (sources, {"gate": "FAIL", "rules": []}, False))
    with pytest.raises(pipeline_main.PipelineRunError, match="Quality gate FAIL"):
        pipeline_main.run_pipeline(RUN_DATE, _config(1), logging.getLogger("fail-gate"),
                                   processed_root=processed)
    assert (result["output_dir"] / "metrics.json").read_bytes() == prior
    attempts = list((processed / "diagnostics" / f"run_date={RUN_DATE}").glob("attempt-*"))
    assert len(attempts) == 1
    assert (attempts[0] / "quality_report.json").is_file()
    assert not (attempts[0] / "metrics.json").exists()
    assert not (attempts[0] / "order_journey.jsonl").exists()


def test_api_failure_does_not_publish(tmp_path, actual_data, monkeypatch):
    sources, _ = actual_data
    monkeypatch.setattr(pipeline_main.extract, "extract_local_sources", lambda *_: sources)
    with pytest.raises(extract.DispatchAPIError):
        pipeline_main.run_pipeline(RUN_DATE, _config(_free_port(), retries=1),
                                   logging.getLogger("api-failure"), processed_root=tmp_path)
    assert not (tmp_path / f"run_date={RUN_DATE}").exists()


def test_publishing_failure_preserves_previous_partition(successful_run, actual_data, monkeypatch):
    result, _, processed, _ = successful_run
    prior = (result["output_dir"] / "metrics.json").read_bytes()
    sources, dispatch = actual_data
    monkeypatch.setattr(pipeline_main.extract, "extract_local_sources", lambda *_: sources)
    monkeypatch.setattr(pipeline_main.extract, "extract_dispatch_api", lambda *_: dispatch)
    original = pipeline_main._write_json

    def fail_metrics(path, payload):
        if path.name == "metrics.json":
            raise OSError("simulated disk write failure")
        return original(path, payload)

    monkeypatch.setattr(pipeline_main, "_write_json", fail_metrics)
    with pytest.raises(OSError, match="simulated"):
        pipeline_main.run_pipeline(RUN_DATE, _config(1), logging.getLogger("publish-failure"),
                                   processed_root=processed)
    assert (result["output_dir"] / "metrics.json").read_bytes() == prior
    assert not list(processed.glob(".run_date=*"))


def test_started_mock_shuts_down_on_error(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(pipeline_main.extract, "extract_local_sources",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic extraction failure")))
    with pytest.raises(RuntimeError, match="synthetic"):
        pipeline_main.run_pipeline(RUN_DATE, _config(port), logging.getLogger("shutdown"),
                                   start_mock=True, processed_root=tmp_path)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def test_occupied_port_is_not_used_or_stopped(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(pipeline_main.PipelineRunError, match="occupied"):
            pipeline_main.run_pipeline(RUN_DATE, _config(port), logging.getLogger("occupied"),
                                       start_mock=True, processed_root=tmp_path)
        assert occupied.fileno() >= 0


def test_cli_returns_nonzero_for_invalid_run_date(capsys):
    assert pipeline_main.main(["--run-date", "invalid"]) == 1
    assert "run failed" in capsys.readouterr().err
