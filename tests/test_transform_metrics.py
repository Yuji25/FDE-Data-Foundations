"""Focused Phase 4 order-grain and metric contracts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from pipeline.clean import clean_sources
from pipeline.extract import extract_local_sources
from pipeline.metrics import calculate_metrics
from pipeline.transform import ModelContractError, build_order_journey


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cleaned():
    sources = extract_local_sources(ROOT / "data/raw", logging.getLogger("phase4-tests"))
    dispatch = pd.DataFrame(json.loads((ROOT / "mock_api/dispatch_data.json").read_text()))
    return clean_sources(sources, dispatch)


@pytest.fixture(scope="module")
def journey(cleaned):
    return build_order_journey(*cleaned)


def test_actual_population_and_unknown_traffic(journey):
    assert len(journey) == journey.order_id.nunique() == 1600
    assert set(journey.loc[journey.traffic_bucket.isna(), "order_id"]) == {
        "O00120", "O00723", "O01302"}
    assert journey.order_source_rows.map(len).max() == 2
    assert not {"name", "email", "customer_message", "detail"} & set(journey.columns)


def test_children_are_aggregated_no_row_multiplication_and_left_join(cleaned):
    sources, report = cleaned
    changed = dict(sources)
    changed["customer_app_actions"] = sources["customer_app_actions"].copy(deep=True)
    target = sources["orders"].order_id.iloc[0]
    extra = changed["customer_app_actions"].iloc[0].copy()
    extra["order_id"] = target
    extra["action_id"] = "synthetic-extra"
    changed["customer_app_actions"] = pd.concat([changed["customer_app_actions"],
                                                    pd.DataFrame([extra])], ignore_index=True)
    model = build_order_journey(changed, report)
    assert len(model) == model.order_id.nunique() == 1600
    original = build_order_journey(sources, report).set_index("order_id")
    revised = model.set_index("order_id")
    assert revised.loc[target, "app_action_count"] == original.loc[target, "app_action_count"] + 1
    missing = dict(sources)
    missing["customer_app_actions"] = sources["customer_app_actions"].iloc[0:0].copy()
    model = build_order_journey(missing, report)
    assert len(model) == 1600
    assert model.app_action_count.eq(0).all()
    assert model.eta_view_count.eq(0).all()


def test_nested_telemetry_flattened_by_order_without_dispatch_conflation(journey, cleaned):
    telemetry = cleaned[0]["driver_events"]
    sample = telemetry.iloc[0].events[0]
    order_id = sample["order_id"]
    expected = [event for events in telemetry.events for event in events if event.get("order_id") == order_id]
    row = journey.set_index("order_id").loc[order_id]
    assert row.telemetry_event_count == len(expected)
    assert row.telemetry_assigned_count == sum(e["type"] == "assigned" for e in expected)
    assert row.telemetry_gps_ping_count == sum(e["type"] == "gps_ping" for e in expected)
    assert "telemetry_first_assigned_at" in journey and "dispatch_assigned_at" in journey


def test_missing_and_invalid_milestones_are_null_and_flagged(cleaned):
    sources, report = cleaned
    changed = dict(sources)
    changed["orders"] = sources["orders"].copy(deep=True)
    at = changed["orders"].index[0]
    changed["orders"].at[at, "pickup_at"] = changed["orders"].at[at, "created_at"] - timedelta(minutes=1)
    at2 = changed["orders"].index[1]
    changed["orders"].at[at2, "pickup_at"] = pd.NaT
    model = build_order_journey(changed, report)
    assert pd.isna(model.loc[at, "creation_to_pickup_min"])
    assert bool(model.loc[at, "creation_to_pickup_invalid_chronology"])
    assert pd.isna(model.loc[at2, "creation_to_pickup_min"])
    assert not bool(model.loc[at2, "creation_to_pickup_invalid_chronology"])


def test_fail_gate_and_duplicate_join_key_block_model(cleaned):
    sources, report = cleaned
    with pytest.raises(ModelContractError):
        build_order_journey(sources, {**report, "gate": "FAIL", "can_continue": False})
    with pytest.raises(TypeError):
        build_order_journey(sources)
    bad = dict(sources)
    bad["dispatch"] = pd.concat([sources["dispatch"], sources["dispatch"].iloc[:1]], ignore_index=True)
    with pytest.raises(ModelContractError):
        build_order_journey(bad, report)


def test_actual_metric_counts_and_json_evidence(journey):
    metrics = calculate_metrics(journey)
    assert metrics["population_orders"] == 1600
    assert (metrics["candidate_operational_ldr"]["numerator"],
            metrics["candidate_operational_ldr"]["denominator"]) == (843, 1495)
    assert (metrics["support_lateness_variant"]["numerator"],
            metrics["support_lateness_variant"]["denominator"]) == (349, 1495)
    assert metrics["traffic_lateness_segmentation"]["unknown_traffic_orders"] == 3
    assert sum(g["eligible_delivered"] for g in metrics["traffic_lateness_segmentation"]["groups"]) == 1495
    json.dumps(metrics)


def test_metric_boundaries_interventions_and_external_label_independence(journey):
    base = journey.iloc[:5].copy(deep=True).reset_index(drop=True)
    origin = datetime(2026, 8, 1, 10)
    base["final_status"] = ["delivered", "delivered", "delivered", "cancelled", "delivered"]
    base["created_at"] = [origin] * 5
    base["pickup_at"] = [origin + timedelta(minutes=5)] * 5
    base["promised_eta"] = [origin + timedelta(minutes=20)] * 5
    base["actual_delivery_at"] = [origin + timedelta(minutes=20), origin + timedelta(minutes=30),
                                   origin + timedelta(minutes=31), origin + timedelta(minutes=40), pd.NaT]
    base["intervention_count"] = [0, 1, 1, 0, 0]
    base["intervention_first_at"] = [pd.NaT, origin + timedelta(minutes=25),
                                     origin + timedelta(minutes=40), pd.NaT, pd.NaT]
    base["traffic_bucket"] = ["low", "high", "high", "high", None]
    base["external_outcome_late_flag"] = [1, 0, 0, 1, 1]
    result = calculate_metrics(base)
    assert (result["candidate_operational_ldr"]["numerator"],
            result["candidate_operational_ldr"]["denominator"]) == (2, 3)
    assert (result["support_lateness_variant"]["numerator"],
            result["support_lateness_variant"]["denominator"]) == (1, 3)
    groups = {x["group"]: x for x in result["intervention_lateness_association"]["groups"]}
    assert groups["recorded_intervention"]["eligible_delivered"] == 2
    assert groups["recorded_intervention"]["late_orders"] == 2
    assert groups["none_recorded"]["eligible_delivered"] == 1
    assert result["intervention_lateness_association"]["first_intervention_timing"]["before_delivery"] == 1
    changed = base.copy(deep=True)
    changed["external_outcome_late_flag"] = 1 - changed["external_outcome_late_flag"]
    assert calculate_metrics(changed) == result
