"""Focused Phase 3 quality rules and cleaning policies."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from pipeline.clean import clean_sources
from pipeline.extract import extract_local_sources
from pipeline.validate import validate_sources

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def actual_sources():
    return extract_local_sources(ROOT / "data/raw", logging.getLogger("quality-tests"))


@pytest.fixture(scope="module")
def dispatch():
    return pd.DataFrame(json.loads((ROOT / "mock_api/dispatch_data.json").read_text()))


def rule(report, rule_id, source):
    return next(x for x in report["rules"] if x["rule_id"] == rule_id and x["source"] == source)


def test_actual_profile_and_gate(actual_sources, dispatch):
    working, report, allowed = validate_sources(actual_sources, dispatch)
    assert working["orders"] is not actual_sources["orders"]
    assert allowed and report["gate"] == "WARN"
    assert report["profiles"]["orders"]["raw_rows"] == 1603
    assert report["profiles"]["orders"]["distinct_primary_keys"] == 1600
    assert report["profiles"]["driver_events"]["nested_event_count"] == 10035
    assert rule(report, "CONFLICTING_ORDER_IDS", "orders")["observed_count"] == 0
    assert rule(report, "UNRESOLVED_TRAFFIC_BUCKET", "orders")["status"] == "WARN"
    assert rule(report, "UNRESOLVED_TRAFFIC_BUCKET", "orders")["observed_count"] == 3
    assert report["profiles"]["orders"]["unresolved_traffic_classifications"] == 3
    assert rule(report, "DELIVERED_WITHOUT_ACTUAL_TIME", "orders")["observed_count"] == 37
    assert rule(report, "MISSING_DELIVERED_EVENT", "order_events")["observed_count"] == 37
    assert rule(report, "OUTCOME_STATUS_DISAGREEMENT", "order_outcomes")["observed_count"] == 0
    assert rule(report, "OUTCOME_DELAY_DISAGREEMENT", "order_outcomes")["observed_count"] == 0
    json.dumps(report)


def test_actual_cleaning_counts_lineage_and_repeatability(actual_sources, dispatch):
    original_order = actual_sources["orders"].copy(deep=True)
    original_ticket = actual_sources["support_tickets"].copy(deep=True)
    first, report = clean_sources(actual_sources, dispatch)
    second, second_report = clean_sources(actual_sources, dispatch)
    assert report["gate"] == "WARN" and report["can_continue"]
    assert report["sources"]["orders"] == second_report["sources"]["orders"]
    assert (report["sources"]["orders"]["original_rows"], report["sources"]["orders"]["cleaned_rows"], report["sources"]["orders"]["quarantined_rows"]) == (1603, 1600, 0)
    assert (report["sources"]["support_tickets"]["original_rows"], report["sources"]["support_tickets"]["cleaned_rows"]) == (202, 201)
    assert (report["sources"]["customer_interactions"]["original_rows"], report["sources"]["customer_interactions"]["cleaned_rows"], report["sources"]["customer_interactions"]["quarantined_rows"]) == (497, 491, 6)
    assert "orders" not in first["_quarantine"]
    assert first["orders"].order_id.nunique() == 1600
    expected = {
        "O00120": ([119, 1600], ["severe", "medium"]),
        "O00723": ([722, 1601], ["medium", "high"]),
        "O01302": ([1301, 1602], ["medium", "high"]),
    }
    resolution = next(action for action in report["sources"]["orders"]["actions"]
                      if action["action"] == "reconcile_noncritical_traffic_conflict")
    assert resolution["rows"] == 3
    assert resolution["unresolved_traffic_classifications"] == 3
    assert set(resolution["reconciled_order_ids"]) == set(expected)
    for item in resolution["evidence"]:
        order_id = item["order_id"]
        source_rows, disputed_values = expected[order_id]
        assert item["source_rows"] == source_rows
        assert item["traffic_values"] == disputed_values
        assert item["differing_columns"] == ["traffic_bucket"]
        canonical = first["orders"].set_index("order_id").loc[order_id]
        assert pd.isna(canonical.traffic_bucket)
        assert canonical._source_rows == source_rows
    assert first["support_tickets"].loc[first["support_tickets"].ticket_id.eq("T00013"), "_source_rows"].iloc[0] == [12, 201]
    assert first["orders"].final_status.isin(["delivered", "cancelled"]).all()
    assert first["orders"].actual_delivery_at.isna().sum() == 105
    assert first["orders"].iloc[0]["created_at"].tzinfo is None
    first_event = first["driver_events"].iloc[0].events[0]
    assert first_event["_raw_timestamp"] == actual_sources["driver_events"].iloc[0].events[0]["timestamp"]
    assert isinstance(first_event["timestamp"], __import__("datetime").datetime)
    assert isinstance(actual_sources["driver_events"].iloc[0].events[0]["timestamp"], str)
    pd.testing.assert_frame_equal(first["orders"], second["orders"])
    pd.testing.assert_frame_equal(actual_sources["orders"], original_order)
    pd.testing.assert_frame_equal(actual_sources["support_tickets"], original_ticket)
    json.dumps(report)


def test_missing_source_column_or_key_fails(actual_sources, dispatch):
    missing_column = dict(actual_sources)
    missing_column["orders"] = actual_sources["orders"].drop(columns="order_id")
    _, report, allowed = validate_sources(missing_column, dispatch)
    assert not allowed
    assert rule(report, "REQUIRED_COLUMNS", "orders")["status"] == "FAIL"
    assert "order_id" in rule(report, "REQUIRED_COLUMNS", "orders")["examples"]

    missing_traffic = dict(actual_sources)
    missing_traffic["orders"] = actual_sources["orders"].drop(columns="traffic_bucket")
    _, traffic_report, allowed = validate_sources(missing_traffic, dispatch)
    assert not allowed
    assert "traffic_bucket" in rule(traffic_report, "REQUIRED_COLUMNS", "orders")["examples"]

    missing_key = dict(actual_sources)
    missing_key["orders"] = actual_sources["orders"].copy()
    missing_key["orders"].loc[0, "order_id"] = None
    _, report, allowed = validate_sources(missing_key, dispatch)
    assert not allowed
    assert rule(report, "MISSING_KEY", "orders")["observed_count"] == 1


def test_exact_order_duplicate_can_be_collapsed(actual_sources, dispatch):
    sources = dict(actual_sources)
    orders = actual_sources["orders"].iloc[:-3].copy()
    sources["orders"] = pd.concat([orders, orders.iloc[[0]].copy()], ignore_index=True)
    cleaned, report = clean_sources(sources, dispatch)
    assert report["can_continue"]
    assert report["gate"] == "WARN"  # existing source limitations remain
    assert report["sources"]["orders"]["cleaned_rows"] == 1600
    assert report["sources"]["orders"]["quarantined_rows"] == 0
    assert cleaned["orders"].iloc[0]["_source_rows"] == [0, 1600]


def test_critical_order_conflict_quarantines_and_closes_gate(actual_sources, dispatch):
    sources = dict(actual_sources)
    orders = actual_sources["orders"].copy(deep=True)
    conflicting = orders.iloc[[0]].copy()
    conflicting.loc[conflicting.index[0], "promised_eta"] = "2026-08-30T00:00:00"
    sources["orders"] = pd.concat([orders, conflicting], ignore_index=True)

    _, quality, allowed = validate_sources(sources, dispatch)
    assert not allowed and quality["gate"] == "FAIL"
    fatal = rule(quality, "CONFLICTING_ORDER_IDS", "orders")
    assert fatal["observed_count"] == 1
    assert fatal["examples"] == [orders.iloc[0].order_id]
    assert rule(quality, "UNRESOLVED_TRAFFIC_BUCKET", "orders")["observed_count"] == 3

    cleaned, report = clean_sources(sources, dispatch)
    assert not report["can_continue"]
    assert report["sources"]["orders"]["quarantined_rows"] == 2
    assert report["sources"]["orders"]["cleaned_rows"] == 1599
    quarantined = cleaned["_quarantine"]["orders"]
    assert quarantined.order_id.tolist() == [orders.iloc[0].order_id] * 2
    assert quarantined._source_row.tolist() == [0, 1603]
    assert orders.iloc[0].order_id not in set(cleaned["orders"].order_id)
    assert sources["orders"].iloc[0].promised_eta == orders.iloc[0].promised_eta


def test_conflicting_child_ids_are_quarantined(actual_sources, dispatch):
    cleaned, report = clean_sources(actual_sources, dispatch)
    assert set(cleaned["_quarantine"]["customer_interactions"].interaction_id) == {"CI-0178", "CI-0179", "CI-0180"}
    assert report["quarantine"]["customer_interactions"]["rows"] == 6
    assert not cleaned["customer_interactions"].interaction_id.duplicated().any()


def test_invalid_missing_time_and_foreign_keys_are_evidenced(actual_sources, dispatch):
    sources = dict(actual_sources)
    orders = actual_sources["orders"].copy()
    orders.loc[0, "promised_eta"] = "not-a-time"
    orders.loc[1, "customer_id"] = "C-NOT-FOUND"
    sources["orders"] = orders
    _, report, _ = validate_sources(sources, dispatch)
    assert rule(report, "INVALID_TIMESTAMP_PROMISED_ETA", "orders")["observed_count"] == 1
    assert rule(report, "FOREIGN_KEY_CUSTOMER_ID", "orders")["observed_count"] == 1
    cleaned, clean_report = clean_sources(sources, dispatch)
    assert pd.isna(cleaned["orders"].iloc[0].promised_eta)
    assert cleaned["orders"].iloc[0]["_raw_promised_eta"] == "not-a-time"
    assert clean_report["sources"]["orders"]["actions"]
    assert sources["orders"].loc[0, "promised_eta"] == "not-a-time"


def test_orphans_missing_events_and_outcome_disagreement(actual_sources, dispatch):
    sources = dict(actual_sources)
    sources["order_events"] = actual_sources["order_events"].loc[lambda d: ~d.order_id.eq("O00001")].copy()
    sources["order_outcomes"] = actual_sources["order_outcomes"].copy()
    sources["order_outcomes"].loc[0, "final_status_norm"] = "cancelled"
    sources["support_tickets"] = pd.concat([actual_sources["support_tickets"], actual_sources["support_tickets"].iloc[[0]].assign(ticket_id="T-ORPHAN", order_id="O-NOT-FOUND")], ignore_index=True)
    telemetry = actual_sources["driver_events"].copy(deep=True)
    telemetry.at[0, "events"] = deepcopy(telemetry.at[0, "events"]) + [{"order_id": "O-NOT-FOUND", "type": "gps_ping", "timestamp": "2026-08-01T12:00:00"}]
    sources["driver_events"] = telemetry
    _, report, _ = validate_sources(sources, dispatch)
    assert rule(report, "ORPHAN_ORDER_REFERENCE", "support_tickets")["observed_count"] == 1
    assert rule(report, "TELEMETRY_ORPHAN_ORDER", "driver_events")["observed_count"] == 1
    assert rule(report, "MISSING_ORDER_CREATED_EVENT", "order_events")["observed_count"] == 1
    assert rule(report, "OUTCOME_STATUS_DISAGREEMENT", "order_outcomes")["observed_count"] == 1


def test_dispatch_coverage_and_nested_structure_fail_closed(actual_sources, dispatch):
    _, report, allowed = validate_sources(actual_sources, dispatch.iloc[:-1].copy())
    assert not allowed
    assert rule(report, "DISPATCH_ORDER_COVERAGE", "dispatch")["observed_count"] == 1
    sources = dict(actual_sources)
    nested = actual_sources["driver_events"].copy(deep=True)
    nested.at[0, "events"] = "broken"
    sources["driver_events"] = nested
    _, report, allowed = validate_sources(sources, dispatch)
    assert not allowed
    assert rule(report, "TELEMETRY_STRUCTURE", "driver_events")["observed_count"] == 1
