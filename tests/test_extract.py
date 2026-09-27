from __future__ import annotations

import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import requests

from pipeline import extract
from pipeline.extract import (
    DispatchAPIError,
    DispatchCompletenessError,
    extract_dispatch_api,
    extract_local_sources,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"


@pytest.fixture
def logger():
    value = logging.getLogger("flasheats-extraction-tests")
    value.handlers.clear()
    value.addHandler(logging.NullHandler())
    value.propagate = False
    return value


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self.responses:
            raise AssertionError("No fake response remains")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def dispatch_record(number: int) -> dict:
    order_id = f"O{number:05d}"
    return {
        "order_id": order_id,
        "driver_id": "D001",
        "original_driver_id": "D001",
        "assigned_at": "2026-08-01T10:00:00",
        "reassigned_at": None,
        "estimated_pickup_at": "2026-08-01T10:15:00",
        "current_delivery_eta": "2026-08-01T10:45:00",
        "dispatch_status": "completed",
        "eta_model_version": "eta-v3.2",
    }


def page_payload(page, records, has_more, total_records, page_size=2):
    return {
        "data": records,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "total_records": total_records,
    }


def config(max_retries=3, retry_backoff_factor=2):
    return SimpleNamespace(
        max_retries=max_retries,
        retry_backoff_factor=retry_backoff_factor,
    )


def install_fake_session(monkeypatch, responses):
    session = FakeSession(responses)
    monkeypatch.setattr(extract.requests, "Session", lambda: session)
    return session


def test_extract_local_sources_loads_every_required_source(logger):
    sources = extract_local_sources(RAW_DATA_DIR, logger)

    expected_rows = {
        "orders": 1603,
        "customers": 900,
        "drivers": 120,
        "restaurants": 60,
        "restaurants_csv": 60,
        "support_tickets": 202,
        "restaurant_status": 502,
        "customer_interactions": 497,
        "customer_app_actions": 2365,
        "order_events": 4955,
        "order_interventions": 430,
        "order_outcomes": 1600,
        "driver_events": 120,
    }
    assert {name: len(sources[name]) for name in expected_rows} == expected_rows
    assert set(sources["metadata"]) == {
        "client_metric_definitions",
        "class7_model_brief",
    }
    assert isinstance(sources["metadata"]["client_metric_definitions"], dict)
    assert isinstance(sources["metadata"]["class7_model_brief"], dict)
    assert isinstance(sources["driver_events"].iloc[0]["events"], list)


def test_dispatch_paginates_and_preserves_raw_pages(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    session = install_fake_session(
        monkeypatch,
        [
            FakeResponse(
                200,
                page_payload(1, [dispatch_record(1), dispatch_record(2)], True, 3),
            ),
            FakeResponse(200, page_payload(2, [dispatch_record(3)], False, 3)),
        ],
    )

    frame = extract_dispatch_api(
        "http://dispatch.test/",
        "2026-09-27",
        config(),
        logger,
    )

    assert len(frame) == 3
    assert frame["order_id"].is_unique
    assert [call["params"]["page"] for call in session.calls] == [1, 2]
    assert session.closed
    partition = tmp_path / "dispatch" / "run_date=2026-09-27"
    assert sorted(path.name for path in partition.iterdir()) == [
        "page_1.json",
        "page_2.json",
    ]
    assert json.loads((partition / "page_1.json").read_text())["page"] == 1
    assert json.loads((partition / "page_2.json").read_text())["page"] == 2


def test_dispatch_retries_http_500_and_429(tmp_path, monkeypatch, logger):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    session = install_fake_session(
        monkeypatch,
        [
            FakeResponse(500, {"error": "temporary"}),
            FakeResponse(429, {"retry_after_seconds": 3}),
            FakeResponse(200, page_payload(1, [dispatch_record(1)], False, 1)),
        ],
    )
    sleeps = []
    monkeypatch.setattr(extract.time, "sleep", sleeps.append)

    frame = extract_dispatch_api(
        "http://dispatch.test",
        "2026-09-27",
        config(max_retries=3, retry_backoff_factor=2),
        logger,
    )

    assert len(frame) == 1
    assert len(session.calls) == 3
    assert sleeps == [1, 3]


def test_dispatch_retries_timeouts_and_connection_errors(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    session = install_fake_session(
        monkeypatch,
        [
            requests.Timeout("slow response"),
            requests.ConnectionError("connection refused"),
            FakeResponse(200, page_payload(1, [dispatch_record(1)], False, 1)),
        ],
    )
    sleeps = []
    monkeypatch.setattr(extract.time, "sleep", sleeps.append)

    frame = extract_dispatch_api(
        "http://dispatch.test",
        "2026-09-27",
        config(max_retries=3, retry_backoff_factor=2),
        logger,
    )

    assert len(frame) == 1
    assert len(session.calls) == 3
    assert sleeps == [1, 2]


def test_dispatch_does_not_retry_other_4xx(tmp_path, monkeypatch, logger):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    session = install_fake_session(
        monkeypatch,
        [FakeResponse(404, {"error": "not found"})],
    )
    sleeps = []
    monkeypatch.setattr(extract.time, "sleep", sleeps.append)

    with pytest.raises(DispatchAPIError, match="non-retryable HTTP 404"):
        extract_dispatch_api(
            "http://dispatch.test",
            "2026-09-27",
            config(),
            logger,
        )

    assert len(session.calls) == 1
    assert sleeps == []
    assert not (tmp_path / "dispatch" / "run_date=2026-09-27").exists()


def test_dispatch_fails_on_reported_count_mismatch(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    install_fake_session(
        monkeypatch,
        [FakeResponse(200, page_payload(1, [dispatch_record(1)], False, 2))],
    )

    with pytest.raises(DispatchCompletenessError, match="record-count mismatch"):
        extract_dispatch_api(
            "http://dispatch.test",
            "2026-09-27",
            config(),
            logger,
        )

    assert not (tmp_path / "dispatch" / "run_date=2026-09-27").exists()


def test_dispatch_fails_on_missing_or_out_of_order_page(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    install_fake_session(
        monkeypatch,
        [FakeResponse(200, page_payload(2, [dispatch_record(1)], False, 1))],
    )

    with pytest.raises(DispatchCompletenessError, match="page sequence mismatch"):
        extract_dispatch_api(
            "http://dispatch.test",
            "2026-09-27",
            config(),
            logger,
        )


def test_dispatch_fails_on_duplicate_order_ids(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    install_fake_session(
        monkeypatch,
        [
            FakeResponse(
                200,
                page_payload(1, [dispatch_record(1), dispatch_record(1)], False, 2),
            )
        ],
    )

    with pytest.raises(DispatchCompletenessError, match="order_id uniqueness failed"):
        extract_dispatch_api(
            "http://dispatch.test",
            "2026-09-27",
            config(),
            logger,
        )


def test_dispatch_fails_on_malformed_response(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    install_fake_session(
        monkeypatch,
        [FakeResponse(200, {"data": [], "page": 1})],
    )

    with pytest.raises(DispatchAPIError, match="missing response fields"):
        extract_dispatch_api(
            "http://dispatch.test",
            "2026-09-27",
            config(),
            logger,
        )


def test_dispatch_rerun_replaces_partition_without_stale_pages(
    tmp_path, monkeypatch, logger
):
    monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
    install_fake_session(
        monkeypatch,
        [
            FakeResponse(200, page_payload(1, [dispatch_record(1)], True, 2)),
            FakeResponse(200, page_payload(2, [dispatch_record(2)], False, 2)),
        ],
    )
    extract_dispatch_api(
        "http://dispatch.test",
        "2026-09-27",
        config(),
        logger,
    )

    replacement = dispatch_record(99)
    install_fake_session(
        monkeypatch,
        [FakeResponse(200, page_payload(1, [replacement], False, 1))],
    )
    frame = extract_dispatch_api(
        "http://dispatch.test",
        "2026-09-27",
        config(),
        logger,
    )

    partition = tmp_path / "dispatch" / "run_date=2026-09-27"
    assert [path.name for path in partition.iterdir()] == ["page_1.json"]
    assert frame["order_id"].tolist() == ["O00099"]
    assert json.loads((partition / "page_1.json").read_text())["data"] == [
        replacement
    ]


def test_integration_with_submission_owned_mock_api(
    tmp_path, monkeypatch, logger
):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mock_api.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"mock API exited early\nstdout={stdout}\nstderr={stderr}")
            try:
                if requests.get(f"{base_url}/health", timeout=0.25).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.05)
        else:
            pytest.fail("mock API did not become healthy")

        monkeypatch.setattr(extract, "DISPATCH_RAW_ROOT", tmp_path / "dispatch")
        frame = extract_dispatch_api(
            base_url,
            "2026-09-27",
            config(max_retries=3, retry_backoff_factor=1),
            logger,
        )

        assert len(frame) == 1600
        assert frame["order_id"].nunique() == 1600
        partition = tmp_path / "dispatch" / "run_date=2026-09-27"
        assert len(list(partition.glob("page_*.json"))) == 8
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
