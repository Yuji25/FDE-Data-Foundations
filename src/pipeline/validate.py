"""Source profiling and evidence-based quality gates for Phase 3."""
from __future__ import annotations

from copy import deepcopy
import pandas as pd

# Columns needed to understand each source without assuming business authority.
SCHEMA = {
    "orders": ("order_id", "customer_id", "restaurant_id", "driver_id", "created_at", "promised_eta", "pickup_at", "actual_delivery_at", "final_status", "traffic_bucket"),
    "customers": ("customer_id",), "drivers": ("driver_id",),
    "restaurants": ("restaurant_id",), "restaurants_csv": ("restaurant_id",),
    "support_tickets": ("ticket_id", "order_id", "created_at"),
    "restaurant_status": ("order_id", "restaurant_id", "status", "last_updated_at"),
    "customer_interactions": ("interaction_id", "order_id", "interaction_at"),
    "customer_app_actions": ("action_id", "order_id", "customer_id", "action_at"),
    "order_events": ("event_id", "order_id", "event_type", "event_time"),
    "order_interventions": ("intervention_id", "order_id", "intervention_at"),
    "order_outcomes": ("order_id", "final_status_norm", "delivered_flag", "late_flag", "delay_min"),
    "driver_events": ("driver_id", "events"),
    "dispatch": ("order_id", "driver_id", "original_driver_id", "assigned_at", "reassigned_at", "estimated_pickup_at", "current_delivery_eta", "dispatch_status", "eta_model_version"),
}
PRIMARY_KEYS = {"orders": "order_id", "customers": "customer_id", "drivers": "driver_id", "restaurants": "restaurant_id", "restaurants_csv": "restaurant_id", "support_tickets": "ticket_id", "customer_interactions": "interaction_id", "customer_app_actions": "action_id", "order_events": "event_id", "order_interventions": "intervention_id", "order_outcomes": "order_id", "driver_events": "driver_id", "dispatch": "order_id"}
TIMESTAMPS = {"orders": ("created_at", "promised_eta", "pickup_at", "actual_delivery_at"), "support_tickets": ("created_at",), "restaurant_status": ("last_updated_at",), "customer_interactions": ("interaction_at",), "customer_app_actions": ("action_at",), "order_events": ("event_time",), "order_interventions": ("intervention_at",), "dispatch": ("assigned_at", "reassigned_at", "estimated_pickup_at", "current_delivery_eta")}
EXAMPLE_LIMIT = 5


def _copy_sources(sources):
    return {name: frame.copy(deep=True) if isinstance(frame, pd.DataFrame) else deepcopy(frame) for name, frame in sources.items()}


def _missing(series):
    return series.isna() | series.astype("string").str.strip().eq("").fillna(False)


def _times(series):
    return pd.to_datetime(series, format="mixed", errors="coerce")


def classify_order_duplicates(orders: pd.DataFrame) -> dict[str, list[dict]]:
    """Compare every original order column; only traffic_bucket may be unresolved.

    Source-row references are zero-based positions in the extracted DataFrame.
    Any disagreement outside traffic_bucket is critical, including a null/value
    mismatch or a different timestamp representation.
    """
    result: dict[str, list[dict]] = {"exact": [], "traffic_only": [], "critical": []}
    if "order_id" not in orders.columns:
        return result
    raw = orders.reset_index(drop=True)
    duplicate = raw[raw.order_id.duplicated(keep=False) & ~_missing(raw.order_id)]
    for order_id, group in duplicate.groupby("order_id", sort=True):
        source_rows = [int(index) for index in group.index]
        first = group.iloc[0]
        differing = []
        for column in raw.columns:
            values = group[column].tolist()
            def same(left, right):
                if bool(pd.isna(left)) and bool(pd.isna(right)):
                    return True
                if bool(pd.isna(left)) or bool(pd.isna(right)):
                    return False
                return bool(left == right)
            if any(not same(first[column], value) for value in values[1:]):
                differing.append(column)
        evidence = {"order_id": str(order_id), "source_rows": source_rows,
                    "differing_columns": differing}
        if not differing:
            result["exact"].append(evidence)
        elif differing == ["traffic_bucket"]:
            evidence["traffic_values"] = [
                None if bool(pd.isna(value)) else str(value)
                for value in group.traffic_bucket.tolist()
            ]
            result["traffic_only"].append(evidence)
        else:
            result["critical"].append(evidence)
    return result


def validate_sources(sources: dict, dispatch: pd.DataFrame | None = None) -> tuple[dict, dict, bool]:
    """Return copied sources, JSON-safe report, and gate boolean.

    Rule objects contain rule_id, source, severity, status, observed_count,
    examples (at most five non-PII identifiers), and explanation. FAIL closes
    the gate; WARN records a quantified defect while permitting processing.
    """
    working = _copy_sources(sources)
    if dispatch is not None:
        working["dispatch"] = dispatch.copy(deep=True)
    report = {"profiles": {}, "rules": [], "gate": "FAIL", "can_continue": False}

    def add(rule_id, source, severity, count, explanation, examples=()):
        count = int(count)
        report["rules"].append({"rule_id": rule_id, "source": source,
            "severity": severity, "status": "PASS" if count == 0 else severity,
            "observed_count": count, "examples": [str(x) for x in list(examples)[:EXAMPLE_LIMIT]],
            "explanation": explanation})

    for name, columns in SCHEMA.items():
        frame = working.get(name)
        if not isinstance(frame, pd.DataFrame):
            add("SOURCE_AVAILABLE", name, "FAIL", 1, "Required extracted DataFrame is missing")
            continue
        missing = sorted(set(columns) - set(frame.columns))
        add("REQUIRED_COLUMNS", name, "FAIL", len(missing), "Required source columns must exist", missing)
        if missing:
            continue
        key = PRIMARY_KEYS.get(name)
        profile = {"raw_rows": len(frame), "primary_key": key,
                   "distinct_primary_keys": int(frame[key].nunique(dropna=True)) if key else None,
                   "duplicate_key_rows": int(frame.duplicated(key, keep=False).sum()) if key else None,
                   "missingness": {col: int(_missing(frame[col]).sum()) for col in columns if col != "events"}}
        report["profiles"][name] = profile
        if key:
            bad = _missing(frame[key]); dup = frame[key].duplicated(keep=False) & ~bad
            add("MISSING_KEY", name, "FAIL", int(bad.sum()), "Primary keys cannot be missing", frame.index[bad].tolist())
            # Orders can contain conflicting duplicates; cleanup checks their content below.
            severity = "FAIL" if name in {"customers", "drivers", "restaurants", "order_outcomes", "dispatch"} else "WARN"
            add("DUPLICATE_KEY", name, severity, int(dup.sum()), "Rows sharing a primary identifier require explicit treatment", frame.loc[dup, key].unique())
        if name == "orders" and len(frame) == 0:
            add("EMPTY_ORDERS", name, "FAIL", 1, "Order population cannot be empty")
        if name == "dispatch" and len(frame) == 0:
            add("EMPTY_DISPATCH", name, "FAIL", 1, "Dispatch snapshot cannot be empty")
        for col in TIMESTAMPS.get(name, ()):
            parsed = _times(frame[col]); bad = ~_missing(frame[col]) & parsed.isna()
            add("INVALID_TIMESTAMP_" + col.upper(), name, "WARN", int(bad.sum()), "Non-null timestamp is unparseable; do not infer a replacement", frame.index[bad].tolist())

    metadata = working.get("metadata")
    absent_metadata = sorted({"client_metric_definitions", "class7_model_brief"} - set(metadata if isinstance(metadata, dict) else ()))
    add("GOVERNANCE_AVAILABLE", "metadata", "FAIL", len(absent_metadata), "Governance documents are required", absent_metadata)
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(x), dict) for x in {"client_metric_definitions", "class7_model_brief"}):
        add("GOVERNANCE_STRUCTURE", "metadata", "FAIL", 1, "Governance documents must be JSON objects")

    def ready(*names):
        return all(name in report["profiles"] for name in names)

    if ready("orders"):
        orders = working["orders"]
        ids = set(orders["order_id"].dropna())
        duplicate_resolution = classify_order_duplicates(orders)
        critical = duplicate_resolution["critical"]
        traffic_only = duplicate_resolution["traffic_only"]
        report["profiles"]["orders"]["reconciled_order_ids"] = [
            item["order_id"] for item in traffic_only
        ]
        report["profiles"]["orders"]["unresolved_traffic_classifications"] = len(traffic_only)
        add("CONFLICTING_ORDER_IDS", "orders", "FAIL", len(critical),
            "Duplicate orders disagree outside traffic_bucket; quarantine every row and close the gate",
            [item["order_id"] for item in critical])
        add("UNRESOLVED_TRAFFIC_BUCKET", "orders", "WARN", len(traffic_only),
            "Only traffic_bucket differs; retain one order with null traffic and both raw values in cleaning evidence",
            [item["order_id"] for item in traffic_only])
        for col in ("promised_eta", "actual_delivery_at"):
            bad = _missing(orders[col])
            add("LDR_NULL_" + col.upper(), "orders", "WARN", int(bad.sum()), "Candidate LDR requires this timestamp; preserve nulls", orders.loc[bad, "order_id"].unique())
        delivered = orders.final_status.astype("string").str.strip().str.lower().eq("delivered")
        missing_delivered = delivered & _missing(orders.actual_delivery_at)
        add("DELIVERED_WITHOUT_ACTUAL_TIME", "orders", "WARN", int(missing_delivered.sum()), "Delivered status without observed delivery time; exclude from candidate LDR denominator pending contract", orders.loc[missing_delivered, "order_id"].unique())
        if ready("order_events"):
            event_rows = working["order_events"]
            created_ids = set(event_rows.loc[event_rows.event_type.eq("ORDER_CREATED"), "order_id"].dropna())
            missing_created = sorted(ids - created_ids)
            add("MISSING_ORDER_CREATED_EVENT", "order_events", "WARN", len(missing_created), "Order has no observed ORDER_CREATED event", missing_created)
            delivered_ids = set(orders.loc[delivered, "order_id"].dropna())
            event_delivered_ids = set(event_rows.loc[event_rows.event_type.eq("DELIVERED"), "order_id"].dropna())
            missing_delivery_event = sorted(delivered_ids - event_delivered_ids)
            add("MISSING_DELIVERED_EVENT", "order_events", "WARN", len(missing_delivery_event), "Delivered order has no DELIVERED event", missing_delivery_event)
        status = orders.final_status.astype("string").str.strip().str.lower()
        bad = ~status.isin(["delivered", "cancelled"])
        add("UNKNOWN_ORDER_STATUS", "orders", "WARN", int(bad.sum()), "Status outside observed delivered/cancelled vocabulary", orders.loc[bad, "order_id"].unique())
        parsed = {col: _times(orders[col]) for col in TIMESTAMPS["orders"]}
        for left, right in (("created_at", "promised_eta"), ("created_at", "pickup_at"), ("pickup_at", "actual_delivery_at")):
            wrong = parsed[left].notna() & parsed[right].notna() & (parsed[left] > parsed[right])
            add("LIFECYCLE_" + left.upper() + "_AFTER_" + right.upper(), "orders", "WARN", int(wrong.sum()), "Observed timestamps violate expected chronology; retain source values", orders.loc[wrong, "order_id"].unique())
        for source, key, dimension in (("orders", "customer_id", "customers"), ("orders", "driver_id", "drivers"), ("orders", "restaurant_id", "restaurants")):
            if ready(dimension):
                bad = ~_missing(orders[key]) & ~orders[key].isin(working[dimension][key])
                add("FOREIGN_KEY_" + key.upper(), source, "WARN", int(bad.sum()), "Non-null dimension key is unmatched", orders.loc[bad, "order_id"].unique())
                null = _missing(orders[key]); add("NULL_FOREIGN_KEY_" + key.upper(), source, "WARN", int(null.sum()), "Dimension key is missing", orders.loc[null, "order_id"].unique())
        for name, key, dimension in (("customer_app_actions", "customer_id", "customers"), ("restaurant_status", "restaurant_id", "restaurants"), ("dispatch", "driver_id", "drivers")):
            if ready(name, dimension):
                frame = working[name]
                bad = ~_missing(frame[key]) & ~frame[key].isin(working[dimension][key])
                add("FOREIGN_KEY_" + key.upper(), name, "WARN", int(bad.sum()), "Non-null dimension key is unmatched", frame.loc[bad, "order_id"].unique())
                null = _missing(frame[key])
                add("NULL_FOREIGN_KEY_" + key.upper(), name, "WARN", int(null.sum()), "Dimension key is missing", frame.loc[null, "order_id"].unique())
        for name in ("support_tickets", "restaurant_status", "customer_interactions", "customer_app_actions", "order_events", "order_interventions", "order_outcomes", "dispatch"):
            if ready(name):
                frame = working[name]; bad = ~_missing(frame.order_id) & ~frame.order_id.isin(ids)
                add("ORPHAN_ORDER_REFERENCE", name, "WARN", int(bad.sum()), "Referenced order ID is absent from orders", frame.loc[bad, "order_id"].unique())
                missing = _missing(frame.order_id)
                add("MISSING_ORDER_REFERENCE", name, "WARN", int(missing.sum()), "Child row lacks an order ID", frame.index[missing].tolist())
        if ready("dispatch"):
            dispatch_ids = set(working["dispatch"].order_id.dropna())
            missing = sorted(ids - dispatch_ids); extra = sorted(dispatch_ids - ids)
            add("DISPATCH_ORDER_COVERAGE", "dispatch", "FAIL", len(missing) + len(extra), "Dispatch IDs must match distinct order population", (missing + extra))
            if len(working["dispatch"]) != len(ids):
                add("DISPATCH_ROW_COUNT", "dispatch", "FAIL", abs(len(working["dispatch"]) - len(ids)), "Dispatch row count must equal distinct order count")
        if ready("order_outcomes"):
            outcomes = working["order_outcomes"]
            comparison = orders.drop_duplicates("order_id").merge(outcomes, on="order_id", how="left")
            norm = comparison.final_status.astype("string").str.strip().str.lower()
            label = comparison.final_status_norm.astype("string").str.strip().str.lower()
            disagree = norm.ne(label).fillna(True)
            add("OUTCOME_STATUS_DISAGREEMENT", "order_outcomes", "WARN", int(disagree.sum()), "External outcome status differs from order status; label authority unverified", comparison.loc[disagree, "order_id"].tolist())
            observed_delay = (_times(comparison.actual_delivery_at) - _times(comparison.promised_eta)).dt.total_seconds() / 60
            external_delay = pd.to_numeric(comparison.delay_min, errors="coerce")
            mismatch = observed_delay.notna() & external_delay.notna() & (observed_delay - external_delay).abs().gt(.01)
            add("OUTCOME_DELAY_DISAGREEMENT", "order_outcomes", "WARN", int(mismatch.sum()), "External delay differs from observed timestamps by more than 0.01 minute", comparison.loc[mismatch, "order_id"].tolist())
            delivered_flag = pd.to_numeric(comparison.delivered_flag, errors="coerce")
            flag_disagreement = delivered_flag.notna() & norm.isin(["delivered", "cancelled"]) & delivered_flag.eq(1).ne(norm.eq("delivered"))
            add("OUTCOME_DELIVERED_FLAG_DISAGREEMENT", "order_outcomes", "WARN", int(flag_disagreement.sum()), "External delivered flag disagrees with normalized order status", comparison.loc[flag_disagreement, "order_id"].tolist())
            flag = pd.to_numeric(comparison.late_flag, errors="coerce")
            mismatch_flag = observed_delay.notna() & flag.notna() & observed_delay.gt(0).ne(flag.eq(1))
            add("OUTCOME_LATE_FLAG_DISAGREEMENT", "order_outcomes", "WARN", int(mismatch_flag.sum()), "External late flag differs from observed >0 minute threshold", comparison.loc[mismatch_flag, "order_id"].tolist())

    if ready("driver_events"):
        telemetry = working["driver_events"]
        bad_structure = []; nested = []; absent_arrival = True
        for row_index, row in telemetry.iterrows():
            events = row.events
            if not isinstance(events, list):
                bad_structure.append(row_index); continue
            for event in events:
                if not isinstance(event, dict) or any(x not in event for x in ("order_id", "type", "timestamp")):
                    bad_structure.append(row_index); continue
                nested.append((row.driver_id, event))
                if event.get("type") == "driver_arrived_at_restaurant": absent_arrival = False
        add("TELEMETRY_STRUCTURE", "driver_events", "FAIL", len(bad_structure), "Nested events require order_id, type and timestamp", bad_structure)
        report["profiles"]["driver_events"]["nested_event_count"] = len(nested)
        if ready("orders"):
            orphan = [e.get("order_id") for _, e in nested if e.get("order_id") not in ids]
            add("TELEMETRY_ORPHAN_ORDER", "driver_events", "WARN", len(orphan), "Nested event order ID is absent from orders", orphan)
        if ready("drivers"):
            valid = set(working["drivers"].driver_id)
            bad = [key for key, _ in nested if key not in valid]
            add("TELEMETRY_ORPHAN_DRIVER", "driver_events", "WARN", len(bad), "Parent driver ID is absent from driver dimension", bad)
        bad_time = [e.get("order_id") for _, e in nested if pd.isna(pd.to_datetime(e.get("timestamp"), errors="coerce"))]
        add("TELEMETRY_INVALID_TIMESTAMP", "driver_events", "WARN", len(bad_time), "Nested timestamp is missing or invalid", bad_time)
        add("MISSING_ARRIVAL_MILESTONE", "driver_events", "WARN", int(absent_arrival), "No observed driver_arrived_at_restaurant event; do not infer it")

    report["can_continue"] = not any(rule["status"] == "FAIL" for rule in report["rules"])
    report["gate"] = "PASS" if report["can_continue"] and not any(rule["status"] == "WARN" for rule in report["rules"]) else "WARN" if report["can_continue"] else "FAIL"
    return working, report, report["can_continue"]
