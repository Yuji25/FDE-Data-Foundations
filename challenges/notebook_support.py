"""Small, notebook-facing helpers built on the assignment's published evidence.

The production extraction/validation/cleaning/model code remains under ``src/pipeline``.
These helpers only load the resulting snapshot and express classroom-specific views.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
RUN_DATE = "2026-09-27"
PUBLISHED = ROOT / "data" / "processed" / f"run_date={RUN_DATE}"


def load_context() -> dict:
    """Load local raw sources plus the pipeline's canonical order-grain evidence."""
    database = (RAW / "flasheats.db").resolve()
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as con:
        result = {
            name: pd.read_sql_query(f'SELECT * FROM "{name}"', con)
            for name in ("orders", "customers", "drivers", "restaurants")
        }
    for name in (
        "support_tickets", "restaurant_status", "customer_app_actions",
        "customer_interactions", "order_events", "order_interventions",
        "order_outcomes",
    ):
        result[name] = pd.read_csv(RAW / f"{name}.csv")
    with (RAW / "driver_events.json").open(encoding="utf-8") as handle:
        result["driver_events"] = json.load(handle)
    with (RAW / "client_metric_definitions.json").open(encoding="utf-8") as handle:
        result["metric_definitions"] = json.load(handle)
    with (RAW / "class7_model_brief.json").open(encoding="utf-8") as handle:
        result["model_brief"] = json.load(handle)
    result["journey"] = pd.read_json(PUBLISHED / "order_journey.jsonl", lines=True)
    for name in ("metrics", "quality_report", "cleaning_report", "run_manifest"):
        with (PUBLISHED / f"{name}.json").open(encoding="utf-8") as handle:
            result[name] = json.load(handle)
    return result


def lateness_view(journey: pd.DataFrame) -> pd.DataFrame:
    """Return canonical orders with timestamp-derived lateness (never external labels)."""
    frame = journey.copy()
    for column in ("created_at", "promised_eta", "pickup_at", "actual_delivery_at"):
        frame[column] = pd.to_datetime(frame[column], format="mixed", errors="coerce")
    frame["delay_min"] = (
        frame["actual_delivery_at"] - frame["promised_eta"]
    ).dt.total_seconds() / 60
    frame["ldr_eligible"] = (
        frame["final_status"].eq("delivered")
        & frame["promised_eta"].notna()
        & frame["actual_delivery_at"].notna()
        & frame["delay_min"].notna()
    )
    frame["late"] = frame["ldr_eligible"] & frame["delay_min"].gt(0)
    frame["late_10"] = frame["ldr_eligible"] & frame["delay_min"].gt(10)
    return frame


def normalize_ticket_category(series: pd.Series) -> pd.Series:
    """Normalize only documented representation variants, keeping semantics distinct."""
    normalized = series.astype("string").str.strip().str.lower().str.replace(" ", "_", regex=False)
    return normalized.replace({"eta_issue": "eta_issue", "late_delivery": "late_delivery"})


def class7_order_model(context: dict) -> pd.DataFrame:
    """Aggregate one-to-many interactions before joining to canonical order outcomes."""
    journey = lateness_view(context["journey"])
    tickets = context["support_tickets"]
    actions = context["customer_app_actions"]
    interventions = context["order_interventions"]

    support = (
        tickets.dropna(subset=["order_id"]).groupby("order_id").size()
        .rename("support_ticket_count").reset_index()
    )
    cancel = (
        actions.assign(cancel_attempted=actions.action_type.eq("CANCEL_ATTEMPTED"))
        .groupby("order_id", as_index=False)["cancel_attempted"].max()
    )
    intervention = (
        interventions.groupby("order_id").agg(
            intervention_count=("intervention_id", "count"),
            intervention_types=("intervention_type", lambda values: tuple(sorted(set(values)))),
        ).reset_index()
    )
    model = journey[[
        "order_id", "customer_id", "restaurant_id", "final_status", "delay_min",
        "ldr_eligible", "late", "late_10",
    ]].copy()
    model = model.merge(support, on="order_id", how="left", validate="one_to_one")
    model = model.merge(cancel, on="order_id", how="left", validate="one_to_one")
    model = model.merge(intervention, on="order_id", how="left", validate="one_to_one")
    model["support_ticket_count"] = model.support_ticket_count.fillna(0).astype(int)
    model["support_opened"] = model.support_ticket_count.gt(0)
    model["cancel_attempted"] = model.cancel_attempted.fillna(False).astype(bool)
    model["intervention_count"] = model.intervention_count.fillna(0).astype(int)
    model["intervention_types"] = model.intervention_types.map(
        lambda value: value if isinstance(value, tuple) else tuple()
    )
    model["late_flag"] = model["late"].where(model["ldr_eligible"], pd.NA).astype("boolean")
    return model


def build_order_timeline(order_id: str, context: dict) -> pd.DataFrame:
    """Union observed time-stamped lifecycle records without inventing milestones."""
    rows: list[dict] = []

    def add(frame, time_col, type_col, actor, source):
        subset = frame.loc[frame.order_id.eq(order_id)]
        for row in subset.itertuples(index=False):
            rows.append({
                "event_time": getattr(row, time_col),
                "event_type": getattr(row, type_col),
                "actor": actor(row) if callable(actor) else actor,
                "source_system": source,
            })

    add(context["order_events"], "event_time", "event_type",
        lambda row: f"{row.actor_type}:{row.actor_id}", "order_events")
    add(context["customer_app_actions"], "action_at", "action_type", "customer", "customer_app")
    add(context["support_tickets"].dropna(subset=["order_id"]), "created_at", "category", "support/customer", "support")
    add(context["restaurant_status"], "last_updated_at", "status", "restaurant", "restaurant_status")
    add(context["order_interventions"], "intervention_at", "intervention_type",
        lambda row: row.initiated_by, "interventions")
    for driver in context["driver_events"]:
        for event in driver["events"]:
            if event.get("order_id") == order_id:
                rows.append({"event_time": event.get("timestamp"), "event_type": event.get("type"),
                             "actor": f"driver:{driver['driver_id']}", "source_system": "driver_telemetry"})
    timeline = pd.DataFrame(rows)
    timeline["event_time"] = pd.to_datetime(timeline.event_time, format="mixed", errors="coerce")
    return timeline.sort_values(["event_time", "source_system", "event_type"], kind="stable").reset_index(drop=True)
