"""Build the gated, one-row-per-order analytical journey.

Every child is reduced to a unique order_id before joining. Missing activity is
zero only for an available, validated source; milestone timestamps remain null.
"""
from __future__ import annotations

import pandas as pd


class ModelContractError(ValueError):
    """A quality gate or order-grain join contract was violated."""


def _unique(frame: pd.DataFrame, key: str, label: str) -> None:
    if key not in frame or frame[key].isna().any() or frame[key].duplicated().any():
        raise ModelContractError(f"{label} requires non-null unique {key}")


def _join(base: pd.DataFrame, child: pd.DataFrame, label: str, key: str = "order_id") -> pd.DataFrame:
    _unique(child, key, label)
    before = len(base)
    result = base.merge(child, on=key, how="left", validate="many_to_one")
    if len(result) != before:
        raise ModelContractError(f"{label} changed order population")
    return result


def _aggregate_events(frame: pd.DataFrame, type_col: str, time_col: str, prefix: str,
                      milestones: dict[str, str] | None = None) -> pd.DataFrame:
    """Count observations and take first observed milestone; no fabricated time."""
    milestones = milestones or {}
    if frame.empty:
        return pd.DataFrame(columns=["order_id", f"{prefix}_count", f"{prefix}_type_counts", *milestones.values()])
    grouped = frame.groupby("order_id", dropna=True, sort=False)
    result = grouped.size().rename(f"{prefix}_count").to_frame()
    result[f"{prefix}_type_counts"] = grouped[type_col].agg(
        lambda values: {str(kind): int(count) for kind, count in
                        values.value_counts(dropna=False).items()})
    for event_type, column in milestones.items():
        subset = frame.loc[frame[type_col].eq(event_type)]
        result[column] = subset.groupby("order_id")[time_col].min()
    return result.reset_index()


def _telemetry(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for parent in frame.itertuples(index=False):
        for event in parent.events:
            rows.append({"order_id": event.get("order_id"), "driver_id": parent.driver_id,
                         "type": event.get("type"), "timestamp": event.get("timestamp")})
    flat = pd.DataFrame(rows, columns=["order_id", "driver_id", "type", "timestamp"])
    result = _aggregate_events(flat, "type", "timestamp", "telemetry_event", {
        "assigned": "telemetry_first_assigned_at", "picked_up": "telemetry_first_pickup_at",
        "delivered": "telemetry_first_delivered_at", "gps_ping": "telemetry_first_gps_at"})
    if not flat.empty:
        result = _join(result, _aggregate_events(flat.loc[flat.type.eq("gps_ping")],
                          "type", "timestamp", "telemetry_gps_ping"), "telemetry GPS")
        for event_type, column in (("assigned", "telemetry_assigned_count"),
                                   ("picked_up", "telemetry_pickup_count"),
                                   ("delivered", "telemetry_delivery_count")):
            counts = flat.loc[flat.type.eq(event_type)].groupby("order_id").size().rename(column).reset_index()
            result = _join(result, counts, column)
    for column in ("telemetry_gps_ping_count", "telemetry_assigned_count",
                   "telemetry_pickup_count", "telemetry_delivery_count"):
        if column not in result:
            result[column] = pd.Series(dtype="int64")

    return result


def _restaurant_status(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["order_id", "restaurant_status_count", "restaurant_latest_status",
                                     "restaurant_status_counts",
                                     "restaurant_latest_status_at"])
    work = frame.copy()
    work["_normalized_status"] = work.status.astype("string").str.strip().str.lower()
    work = work.sort_values(["order_id", "last_updated_at", "_source_row"],
                            ascending=[True, True, True], na_position="first", kind="stable")
    latest = work.drop_duplicates("order_id", keep="last")[["order_id", "_normalized_status", "last_updated_at"]]
    latest = latest.rename(columns={"_normalized_status": "restaurant_latest_status",
                                    "last_updated_at": "restaurant_latest_status_at"})
    counts = frame.groupby("order_id").size().rename("restaurant_status_count").reset_index()
    status_counts = work.groupby("order_id")["_normalized_status"].agg(
        lambda values: {str(status): int(count) for status, count in
                        values.value_counts(dropna=False).items()}).rename("restaurant_status_counts").reset_index()
    counts = _join(counts, status_counts, "restaurant status counts")
    return _join(counts, latest, "latest restaurant status")


def _counts(frame: pd.DataFrame, prefix: str, time_col: str | None = None,
            type_col: str | None = None) -> pd.DataFrame:
    if frame.empty:
        columns = ["order_id", f"{prefix}_count"]
        if time_col: columns.append(f"{prefix}_first_at")
        if type_col: columns.append(f"{prefix}_types")
        return pd.DataFrame(columns=columns)
    grouped = frame.groupby("order_id", dropna=True, sort=False)
    result = grouped.size().rename(f"{prefix}_count").to_frame()
    if time_col:
        result[f"{prefix}_first_at"] = grouped[time_col].min()
    if type_col:
        result[f"{prefix}_types"] = grouped[type_col].agg(
            lambda values: tuple(sorted(set(values.dropna().astype(str)))))
    return result.reset_index()


def _minutes(start: pd.Series, end: pd.Series) -> tuple[pd.Series, pd.Series]:
    # Mixed timezone-aware and naive values cannot safely be compared.
    values, invalid = [], []
    for left, right in zip(start, end):
        if pd.isna(left) or pd.isna(right):
            values.append(float("nan")); invalid.append(False); continue
        try:
            minutes = (right - left).total_seconds() / 60
        except (TypeError, ValueError):
            values.append(float("nan")); invalid.append(True); continue
        invalid.append(minutes < 0)
        values.append(minutes if minutes >= 0 else float("nan"))
    return pd.Series(values, index=start.index), pd.Series(invalid, index=start.index)


def build_order_journey(cleaned_sources: dict, cleaning_report: dict) -> pd.DataFrame:
    """Return a gated order-grain model; the cleaning report is mandatory.

    A WARN gate is allowed; FAIL and absent/invalid reports are rejected. This
    does not publish or persist the returned DataFrame.
    """
    if not isinstance(cleaning_report, dict) or cleaning_report.get("can_continue") is not True \
            or cleaning_report.get("gate") not in {"PASS", "WARN"}:
        raise ModelContractError("Cleaning quality gate does not permit an order journey")
    required = ("orders", "customers", "drivers", "restaurants", "order_events",
                "driver_events", "restaurant_status", "customer_app_actions",
                "customer_interactions", "support_tickets", "order_interventions",
                "dispatch", "order_outcomes")
    if any(not isinstance(cleaned_sources.get(name), pd.DataFrame) for name in required):
        raise ModelContractError("Required cleaned source is unavailable")
    orders = cleaned_sources["orders"]
    _unique(orders, "order_id", "canonical orders")
    model = orders[["order_id", "customer_id", "restaurant_id", "driver_id", "city",
                    "created_at", "promised_eta", "pickup_at", "actual_delivery_at",
                    "final_status", "distance_km_estimate", "traffic_bucket",
                    "weather_bucket", "_source_rows"]].copy()
    model = model.rename(columns={"_source_rows": "order_source_rows"})
    for name, key, fields in (
        ("customers", "customer_id", ()),
        ("drivers", "driver_id", ("rating", "vehicle_type", "experience_months")),
        ("restaurants", "restaurant_id", ("cuisine", "manual_status_updates"))):
        dim = cleaned_sources[name]
        _unique(dim, key, name)
        model = _join(model, dim[[key, *fields]], name, key)
    events = cleaned_sources["order_events"]
    model = _join(model, _aggregate_events(events, "event_type", "event_time", "order_event", {
        "ORDER_CREATED": "event_first_created_at", "PICKED_UP": "event_first_pickup_at",
        "DELIVERED": "event_first_delivered_at"}), "order events")
    model = _join(model, _telemetry(cleaned_sources["driver_events"]), "driver telemetry")
    model = _join(model, _restaurant_status(cleaned_sources["restaurant_status"]), "restaurant status")
    actions = cleaned_sources["customer_app_actions"]
    model = _join(model, _counts(actions, "app_action", "action_at"), "app actions")
    model = _join(model, _counts(actions.loc[actions.action_type.eq("ETA_VIEWED")],
                                 "eta_view"), "ETA views")
    for name, prefix, time_col, type_col in (
        ("customer_interactions", "interaction", "interaction_at", "interaction_type"),
        ("support_tickets", "ticket", "created_at", "category"),
        ("order_interventions", "intervention", "intervention_at", "intervention_type")):
        model = _join(model, _counts(cleaned_sources[name], prefix, time_col, type_col), name)
    for name, fields in (
        ("dispatch", ("driver_id", "original_driver_id", "assigned_at", "reassigned_at",
                      "estimated_pickup_at", "current_delivery_eta", "dispatch_status", "eta_model_version")),
        ("order_outcomes", ("final_status_norm", "delivered_flag", "late_flag", "delay_min", "outcome_bucket"))):
        source = cleaned_sources[name]
        _unique(source, "order_id", name)
        prefix = "dispatch_" if name == "dispatch" else "external_outcome_"
        renamed = source[["order_id", *fields]].rename(columns={field: prefix + field for field in fields})
        model = _join(model, renamed, name)
    # Child counts are known zero because each required source was available.
    for column in ("order_event_count", "telemetry_event_count", "telemetry_gps_ping_count",
                   "restaurant_status_count", "app_action_count", "eta_view_count",
                   "telemetry_assigned_count", "telemetry_pickup_count", "telemetry_delivery_count",
                   "interaction_count", "ticket_count", "intervention_count"):
        model[column] = model[column].fillna(0).astype(int)
    model["dispatch_reassigned"] = model.dispatch_reassigned_at.notna() | (
        model.dispatch_original_driver_id.notna() & model.dispatch_driver_id.notna() &
        model.dispatch_original_driver_id.ne(model.dispatch_driver_id))
    for prefix, start, end in (("creation_to_pickup", "created_at", "pickup_at"),
                               ("pickup_to_delivery", "pickup_at", "actual_delivery_at")):
        model[prefix + "_min"], model[prefix + "_invalid_chronology"] = _minutes(model[start], model[end])
    model["invalid_observed_chronology"] = model.creation_to_pickup_invalid_chronology | model.pickup_to_delivery_invalid_chronology
    _unique(model, "order_id", "order journey")
    if len(model) != len(orders):
        raise ModelContractError("Order journey changed canonical order population")
    return model
