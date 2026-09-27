"""Traceable cleaning policies; no order-level joins or metric calculation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import pandas as pd

from .validate import PRIMARY_KEYS, TIMESTAMPS, _missing, classify_order_duplicates, validate_sources


def _normalized_time(value):
    if pd.isna(value) or str(value).strip() == "": return pd.NaT
    try:
        # Naive source timestamps remain naive; offsets remain explicit.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return pd.NaT


def clean_sources(sources: dict, dispatch: pd.DataFrame | None = None) -> tuple[dict, dict]:
    """Return cleaned copies and JSON-safe action/quarantine report.

    A FAIL quality gate blocks downstream use of the result. This function still
    returns inspectable, unambiguous rows and quarantined conflicting rows.
    """
    working, quality, allowed = validate_sources(sources, dispatch)
    cleaned = {}
    report = {"gate": quality["gate"], "can_continue": allowed,
              "quality": quality, "sources": {}, "quarantine": {}}
    quarantined_frames = {}
    for name, value in working.items():
        if not isinstance(value, pd.DataFrame):
            cleaned[name] = deepcopy(value)
            continue
        frame = value.copy(deep=True)
        frame.insert(0, "_source_row", list(range(len(frame))))
        frame.insert(1, "_source_rows", [[i] for i in range(len(frame))])
        key = PRIMARY_KEYS.get(name)
        quarantine = pd.DataFrame(columns=frame.columns)
        actions = []
        if key and key in frame:
            duplicated = frame[key].duplicated(keep=False) & ~_missing(frame[key])
            conflicts = []
            exact = []
            traffic_only = []
            if name == "orders":
                resolution = classify_order_duplicates(value)
                conflicts = [item["order_id"] for item in resolution["critical"]]
                exact = [item["order_id"] for item in resolution["exact"]]
                traffic_only = resolution["traffic_only"]
            else:
                for identifier, group in frame.loc[duplicated].groupby(key, sort=False):
                    original_cols = list(value.columns)
                    if len(group[original_cols].drop_duplicates()) == 1:
                        exact.append(identifier)
                    else:
                        conflicts.append(identifier)
            if conflicts:
                mask = frame[key].isin(conflicts)
                quarantine = frame.loc[mask].copy(deep=True)
                frame = frame.loc[~mask].copy(deep=True)
                actions.append({"action": "quarantine_conflicting_key", "rows": len(quarantine),
                                "identifiers": [str(x) for x in conflicts[:5]],
                                "reason": "Rows reuse an identifier but disagree in critical source fields"})
            if traffic_only:
                for item in traffic_only:
                    group = frame.loc[frame[key].eq(item["order_id"])]
                    first = group.index[0]
                    frame.at[first, "traffic_bucket"] = None
                    frame.at[first, "_source_rows"] = item["source_rows"]
                    frame = frame.drop(index=group.index[1:])
                actions.append({"action": "reconcile_noncritical_traffic_conflict",
                                "rows": len(traffic_only),
                                "reconciled_order_ids": [item["order_id"] for item in traffic_only],
                                "unresolved_traffic_classifications": len(traffic_only),
                                "evidence": traffic_only,
                                "reason": "Every other raw order column agrees; traffic is unknown, not a category"})
            if exact:
                mask = frame[key].isin(exact)
                for identifier, group in frame.loc[mask].groupby(key, sort=False):
                    first = group.index[0]
                    frame.at[first, "_source_rows"] = group["_source_row"].tolist()
                before = len(frame)
                frame = frame.drop_duplicates(subset=key, keep="first").copy(deep=True)
                actions.append({"action": "collapse_exact_duplicate", "rows": before-len(frame),
                                "identifiers": [str(x) for x in exact[:5]],
                                "reason": "All original fields agree; retained row lists all source rows"})
        if name == "orders" and "final_status" in frame:
            raw = frame.final_status.copy()
            known = raw.astype("string").str.strip().str.lower().isin(["delivered", "cancelled"])
            changed = known & raw.ne(raw.astype("string").str.strip().str.lower())
            frame["_raw_final_status"] = raw
            frame.loc[known, "final_status"] = raw.loc[known].astype("string").str.strip().str.lower()
            if changed.any():
                actions.append({"action": "normalize_known_status", "rows": int(changed.sum()),
                                "reason": "Case/whitespace variant of observed delivered or cancelled"})
        for col in TIMESTAMPS.get(name, ()):
            if col not in frame: continue
            frame["_raw_" + col] = frame[col].copy()
            frame[col] = pd.Series([_normalized_time(v) for v in frame[col]], index=frame.index, dtype="object")
            invalid = frame["_raw_" + col].notna() & frame[col].isna()
            if invalid.any():
                actions.append({"action": "invalid_timestamp_to_null", "column": col,
                                "rows": int(invalid.sum()), "reason": "Raw value retained in _raw_ column"})
        if name == "driver_events" and "events" in frame:
            # Keep nesting and preserve each raw timestamp alongside the parsed value.
            def normalize_events(events):
                if not isinstance(events, list):
                    return deepcopy(events)
                normalized = deepcopy(events)
                for event in normalized:
                    if isinstance(event, dict) and "timestamp" in event:
                        event["_raw_timestamp"] = event["timestamp"]
                        event["timestamp"] = _normalized_time(event["timestamp"])
                return normalized
            frame["events"] = frame["events"].map(normalize_events)
        cleaned[name] = frame.reset_index(drop=True)
        if len(quarantine):
            quarantined_frames[name] = quarantine.reset_index(drop=True)
        report["quarantine"][name] = {"rows": len(quarantine),
            "source_rows": quarantine["_source_row"].astype(int).tolist(),
            "identifiers": [str(x) for x in quarantine[key].dropna().unique()[:5]] if key and key in quarantine else []}
        report["sources"][name] = {"original_rows": len(value), "cleaned_rows": len(frame),
                                   "quarantined_rows": len(quarantine), "actions": actions}
        if name == "orders":
            report["sources"][name]["reconciled_order_ids"] = [item["order_id"] for item in traffic_only]
            report["sources"][name]["unresolved_traffic_classifications"] = len(traffic_only)
    cleaned["_quarantine"] = quarantined_frames
    return cleaned, report
