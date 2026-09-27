"""Extraction functions for the FlashEats source systems."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any
from uuid import uuid4

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "dispatch"
DISPATCH_PAGE_SIZE = 200
REQUEST_TIMEOUT_SECONDS = 5
MAX_API_PAGES = 10_000

SQLITE_TABLES = ("orders", "customers", "drivers", "restaurants")
CSV_SOURCES = {
    "restaurants_csv": "restaurants.csv",
    "support_tickets": "support_tickets.csv",
    "restaurant_status": "restaurant_status.csv",
    "customer_interactions": "customer_interactions.csv",
    "customer_app_actions": "customer_app_actions.csv",
    "order_events": "order_events.csv",
    "order_interventions": "order_interventions.csv",
    "order_outcomes": "order_outcomes.csv",
}
GOVERNANCE_SOURCES = {
    "client_metric_definitions": "client_metric_definitions.json",
    "class7_model_brief": "class7_model_brief.json",
}
DISPATCH_RECORD_FIELDS = {
    "order_id",
    "driver_id",
    "original_driver_id",
    "assigned_at",
    "reassigned_at",
    "estimated_pickup_at",
    "current_delivery_eta",
    "dispatch_status",
    "eta_model_version",
}


class ExtractionError(RuntimeError):
    """Base class for extraction failures."""


class LocalSourceExtractionError(ExtractionError):
    """Raised when a local source cannot be read as supplied."""


class DispatchAPIError(ExtractionError):
    """Raised for Dispatch API transport, status, or response failures."""


class DispatchCompletenessError(DispatchAPIError):
    """Raised when Dispatch pagination or record completeness is not proven."""


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Required source file not found: {path}")


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise LocalSourceExtractionError(
            f"Malformed JSON source {path}: {exc}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise LocalSourceExtractionError(
            f"Could not read JSON source {path}: {exc}"
        ) from exc


def _read_driver_events(path: Path) -> pd.DataFrame:
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise LocalSourceExtractionError(
            f"Driver telemetry must be a JSON list: {path}"
        )

    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise LocalSourceExtractionError(
                f"Driver telemetry record {index} is not an object: {path}"
            )
        if "driver_id" not in record or not isinstance(record.get("events"), list):
            raise LocalSourceExtractionError(
                f"Driver telemetry record {index} must contain driver_id and an events list: {path}"
            )
        if not all(isinstance(event, dict) for event in record["events"]):
            raise LocalSourceExtractionError(
                f"Driver telemetry record {index} contains a non-object event: {path}"
            )

    # Keep the events lists nested. Flattening belongs to transformation.
    return pd.DataFrame.from_records(payload)


def extract_local_sources(raw_data_dir: Path, logger) -> dict[str, Any]:
    """Load every local source without deduplication or cleaning.

    Operational records are returned as DataFrames. The two governance JSON
    documents are returned as dictionaries under the ``metadata`` key.
    """
    raw_data_dir = Path(raw_data_dir)
    if not raw_data_dir.is_dir():
        raise FileNotFoundError(f"Raw data directory not found: {raw_data_dir}")

    required_paths = [raw_data_dir / "flasheats.db"]
    required_paths.extend(raw_data_dir / name for name in CSV_SOURCES.values())
    required_paths.append(raw_data_dir / "driver_events.json")
    required_paths.extend(
        raw_data_dir / name for name in GOVERNANCE_SOURCES.values()
    )
    for path in required_paths:
        _require_file(path)

    sources: dict[str, Any] = {}
    database_path = (raw_data_dir / "flasheats.db").resolve()
    database_uri = f"{database_path.as_uri()}?mode=ro"

    try:
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            for table in SQLITE_TABLES:
                try:
                    frame = pd.read_sql_query(
                        f'SELECT * FROM "{table}"',
                        connection,
                    )
                except (sqlite3.Error, pd.errors.DatabaseError) as exc:
                    raise LocalSourceExtractionError(
                        f"Could not read SQLite table {table!r} from {database_path}: {exc}"
                    ) from exc
                sources[table] = frame
                logger.info(
                    "Extracted local source | source=sqlite:%s rows=%s",
                    table,
                    len(frame),
                )
    except LocalSourceExtractionError:
        raise
    except sqlite3.Error as exc:
        raise LocalSourceExtractionError(
            f"Could not open SQLite source {database_path} in read-only mode: {exc}"
        ) from exc

    for source_name, filename in CSV_SOURCES.items():
        path = raw_data_dir / filename
        try:
            frame = pd.read_csv(path)
        except (OSError, UnicodeError, pd.errors.ParserError) as exc:
            raise LocalSourceExtractionError(
                f"Could not read CSV source {path}: {exc}"
            ) from exc
        sources[source_name] = frame
        logger.info(
            "Extracted local source | source=%s rows=%s",
            filename,
            len(frame),
        )

    driver_path = raw_data_dir / "driver_events.json"
    driver_events = _read_driver_events(driver_path)
    sources["driver_events"] = driver_events
    logger.info(
        "Extracted local source | source=%s rows=%s nested_events=%s",
        driver_path.name,
        len(driver_events),
        sum(len(events) for events in driver_events["events"]),
    )

    metadata: dict[str, dict[str, Any]] = {}
    for source_name, filename in GOVERNANCE_SOURCES.items():
        path = raw_data_dir / filename
        document = _read_json(path)
        if not isinstance(document, dict):
            raise LocalSourceExtractionError(
                f"Governance source must be a JSON object: {path}"
            )
        metadata[source_name] = document
        logger.info(
            "Extracted governance source | source=%s documents=1",
            filename,
        )
    sources["metadata"] = metadata

    return sources


def _retry_delay_seconds(response, attempt: int, backoff_factor: float) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is None and response.status_code == 429:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            retry_after = body.get("retry_after_seconds")

    if retry_after is not None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = -1
        if delay >= 0:
            return delay

    return backoff_factor ** (attempt - 1)


def _request_dispatch_page(
    session: requests.Session,
    endpoint: str,
    page: int,
    max_attempts: int,
    backoff_factor: float,
    logger,
) -> dict[str, Any]:
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(
                endpoint,
                params={"page": page, "page_size": DISPATCH_PAGE_SIZE},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_attempts:
                raise DispatchAPIError(
                    f"Dispatch page {page} failed after {max_attempts} attempts: {exc}"
                ) from exc
            delay = backoff_factor ** (attempt - 1)
            logger.warning(
                "Retryable Dispatch connection failure | page=%s attempt=%s/%s wait=%.1fs error=%s",
                page,
                attempt,
                max_attempts,
                delay,
                exc,
            )
            time.sleep(delay)
            continue
        except requests.RequestException as exc:
            raise DispatchAPIError(
                f"Dispatch request failed for page {page}: {exc}"
            ) from exc

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise DispatchAPIError(
                    f"Dispatch page {page} returned malformed JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise DispatchAPIError(
                    f"Dispatch page {page} response must be a JSON object"
                )
            return payload

        if response.status_code in {429, 500}:
            if attempt == max_attempts:
                raise DispatchAPIError(
                    f"Dispatch page {page} failed after {max_attempts} attempts; "
                    f"last_status={response.status_code}"
                )
            delay = _retry_delay_seconds(response, attempt, backoff_factor)
            logger.warning(
                "Retryable Dispatch HTTP failure | page=%s status=%s attempt=%s/%s wait=%.1fs",
                page,
                response.status_code,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)
            continue

        if 400 <= response.status_code < 500:
            raise DispatchAPIError(
                f"Dispatch page {page} returned non-retryable HTTP {response.status_code}"
            )
        raise DispatchAPIError(
            f"Dispatch page {page} returned unexpected HTTP {response.status_code}"
        )

    raise AssertionError("bounded retry loop terminated unexpectedly")


def _validate_page_payload(
    payload: dict[str, Any],
    requested_page: int,
    seen_pages: set[int],
    expected_total: int | None,
) -> tuple[list[dict[str, Any]], int, bool, int]:
    required = {"data", "page", "page_size", "has_more", "total_records"}
    missing = sorted(required.difference(payload))
    if missing:
        raise DispatchAPIError(
            f"Dispatch page {requested_page} is missing response fields: {missing}"
        )

    response_page = payload["page"]
    if isinstance(response_page, bool) or not isinstance(response_page, int):
        raise DispatchAPIError("Dispatch response field 'page' must be an integer")
    if response_page != requested_page:
        raise DispatchCompletenessError(
            f"Dispatch page sequence mismatch: requested={requested_page} received={response_page}"
        )
    if response_page in seen_pages:
        raise DispatchCompletenessError(
            f"Dispatch page {response_page} was retrieved more than once"
        )

    page_size = payload["page_size"]
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise DispatchAPIError("Dispatch response field 'page_size' must be a positive integer")

    has_more = payload["has_more"]
    if not isinstance(has_more, bool):
        raise DispatchAPIError("Dispatch response field 'has_more' must be boolean")

    total_records = payload["total_records"]
    if (
        isinstance(total_records, bool)
        or not isinstance(total_records, int)
        or total_records < 0
    ):
        raise DispatchAPIError(
            "Dispatch response field 'total_records' must be a non-negative integer"
        )
    if expected_total is not None and total_records != expected_total:
        raise DispatchCompletenessError(
            "Dispatch total_records changed during pagination: "
            f"first={expected_total} page_{response_page}={total_records}"
        )

    records = payload["data"]
    if not isinstance(records, list):
        raise DispatchAPIError("Dispatch response field 'data' must be a list")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise DispatchAPIError(
                f"Dispatch page {response_page} record {index} is not an object"
            )
        missing_record_fields = sorted(DISPATCH_RECORD_FIELDS.difference(record))
        if missing_record_fields:
            raise DispatchAPIError(
                f"Dispatch page {response_page} record {index} is missing fields: "
                f"{missing_record_fields}"
            )

    if has_more and not records:
        raise DispatchCompletenessError(
            f"Dispatch page {response_page} is empty but has_more is true"
        )

    return records, response_page, has_more, total_records


def _preserve_page(directory: Path, page: int, payload: dict[str, Any]) -> None:
    destination = directory / f"page_{page}.json"
    temporary = directory / f".page_{page}.json.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise DispatchAPIError(
            f"Could not preserve raw Dispatch page {page} at {destination}: {exc}"
        ) from exc


def _publish_snapshot(staging: Path, destination: Path) -> None:
    backup = destination.with_name(
        f".{destination.name}.backup-{uuid4().hex}"
    )
    had_previous = destination.exists()
    if destination.is_symlink() or (had_previous and not destination.is_dir()):
        raise DispatchAPIError(
            f"Dispatch run partition is not a normal directory: {destination}"
        )

    if had_previous:
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except OSError:
        if had_previous and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def _validated_run_date(run_date: str) -> str:
    try:
        parsed = date.fromisoformat(run_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != run_date:
        raise ValueError("run_date must use YYYY-MM-DD")
    return run_date


def extract_dispatch_api(
    api_url: str,
    run_date: str,
    config,
    logger,
) -> pd.DataFrame:
    """Retrieve and preserve one complete Dispatch API snapshot."""
    run_date = _validated_run_date(run_date)
    api_url = api_url.rstrip("/")
    if not api_url:
        raise ValueError("api_url must not be empty")

    try:
        max_attempts = int(config.max_retries)
        backoff_factor = float(config.retry_backoff_factor)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "config must provide numeric max_retries and retry_backoff_factor"
        ) from exc
    if max_attempts < 1:
        raise ValueError("config.max_retries must be at least 1")
    if backoff_factor < 0:
        raise ValueError("config.retry_backoff_factor must be non-negative")

    raw_root = Path(DISPATCH_RAW_ROOT)
    raw_root.mkdir(parents=True, exist_ok=True)
    final_partition = raw_root / f"run_date={run_date}"
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".run_date={run_date}-",
            dir=raw_root,
        )
    )

    endpoint = f"{api_url}/dispatch/orders"
    session = requests.Session()
    records: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    expected_total: int | None = None
    page = 1

    try:
        while True:
            if page > MAX_API_PAGES:
                raise DispatchCompletenessError(
                    f"Dispatch pagination exceeded safety limit of {MAX_API_PAGES} pages"
                )

            payload = _request_dispatch_page(
                session=session,
                endpoint=endpoint,
                page=page,
                max_attempts=max_attempts,
                backoff_factor=backoff_factor,
                logger=logger,
            )
            # Preserve the successful response before interpreting its records.
            _preserve_page(staging, page, payload)

            page_records, response_page, has_more, reported_total = (
                _validate_page_payload(
                    payload=payload,
                    requested_page=page,
                    seen_pages=seen_pages,
                    expected_total=expected_total,
                )
            )
            if expected_total is None:
                expected_total = reported_total

            seen_pages.add(response_page)
            records.extend(page_records)
            logger.info(
                "Extracted Dispatch page | page=%s rows=%s cumulative_rows=%s reported_total=%s",
                response_page,
                len(page_records),
                len(records),
                expected_total,
            )

            if has_more and len(records) >= expected_total:
                raise DispatchCompletenessError(
                    "Dispatch API reported has_more=true after the reported total "
                    f"was reached: retrieved={len(records)} total={expected_total}"
                )
            if not has_more:
                break
            page += 1

        expected_pages = set(range(1, page + 1))
        if seen_pages != expected_pages:
            raise DispatchCompletenessError(
                f"Dispatch page coverage mismatch: expected={sorted(expected_pages)} "
                f"received={sorted(seen_pages)}"
            )
        if expected_total is None or len(records) != expected_total:
            raise DispatchCompletenessError(
                f"Dispatch record-count mismatch: retrieved={len(records)} "
                f"reported_total={expected_total}"
            )

        order_ids = [record.get("order_id") for record in records]
        missing_order_ids = sum(
            not isinstance(order_id, str) or not order_id.strip()
            for order_id in order_ids
        )
        valid_order_ids = [
            order_id
            for order_id in order_ids
            if isinstance(order_id, str) and order_id.strip()
        ]
        duplicate_order_ids = sorted(
            order_id
            for order_id, count in Counter(valid_order_ids).items()
            if count > 1
        )
        logger.info(
            "Dispatch extraction evidence | pages=%s records=%s unique_order_ids=%s "
            "missing_order_ids=%s duplicate_order_ids=%s",
            len(seen_pages),
            len(records),
            len(set(valid_order_ids)),
            missing_order_ids,
            len(duplicate_order_ids),
        )
        if missing_order_ids or duplicate_order_ids:
            raise DispatchCompletenessError(
                "Dispatch order_id uniqueness failed: "
                f"missing={missing_order_ids} duplicates={duplicate_order_ids[:10]}"
            )

        try:
            _publish_snapshot(staging, final_partition)
        except OSError as exc:
            raise DispatchAPIError(
                f"Could not publish Dispatch snapshot to {final_partition}: {exc}"
            ) from exc
        logger.info(
            "Published Dispatch raw snapshot | run_date=%s pages=%s path=%s",
            run_date,
            len(seen_pages),
            final_partition,
        )
        return pd.DataFrame.from_records(records)
    finally:
        session.close()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
