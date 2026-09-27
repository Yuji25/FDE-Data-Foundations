"""Five descriptive, unapproved late-delivery analyses from the order journey."""
from __future__ import annotations

import pandas as pd


def _minutes(left, right):
    try:
        if pd.isna(left) or pd.isna(right):
            return None
        return (right - left).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def _rate(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _late_group(frame: pd.DataFrame, eligible: pd.Series, late: pd.Series,
                group: pd.Series) -> list[dict]:
    rows = []
    for label in sorted(group.unique()):
        selected = group.eq(label)
        denominator = int((selected & eligible).sum())
        numerator = int((selected & late).sum())
        rows.append({"group": str(label), "orders": int(selected.sum()),
                     "eligible_delivered": denominator, "late_orders": numerator,
                     "late_rate": _rate(numerator, denominator),
                     "excluded_orders": int((selected & ~eligible).sum())})
    return rows


def calculate_metrics(order_journey: pd.DataFrame) -> dict:
    """Return JSON-serializable evidence; never use external outcome flags as truth.

    Eligible LDR rows are delivered orders with both order timestamps present.
    Naive timestamps are compared as supplied; mixed aware/naive pairs are
    excluded and counted. Cancellation is excluded; refund status is unknown.
    """
    frame = order_journey
    if frame.order_id.isna().any() or frame.order_id.duplicated().any():
        raise ValueError("Metrics require one non-null row per order_id")
    required = {"final_status", "promised_eta", "actual_delivery_at", "created_at", "pickup_at",
                "creation_to_pickup_min", "pickup_to_delivery_min", "intervention_count",
                "intervention_first_at", "traffic_bucket"}
    absent = required - set(frame.columns)
    if absent:
        raise ValueError(f"Missing order-journey columns: {sorted(absent)}")
    delivered = frame.final_status.eq("delivered").fillna(False)
    delay = frame.apply(lambda row: _minutes(row.promised_eta, row.actual_delivery_at), axis=1)
    eligible = delivered & frame.promised_eta.notna() & frame.actual_delivery_at.notna() & delay.notna()
    late = eligible & delay.gt(0)
    late_10 = eligible & delay.gt(10)
    delivered_count = int(delivered.sum())
    denominator = int(eligible.sum())
    candidate = {"metric_name": "candidate_operational_ldr", "business_question": "How often are delivered orders later than promised?",
                 "definition_status": "unapproved", "numerator": int(late.sum()), "denominator": denominator,
                 "eligibility": "final_status=delivered and promised_eta and actual_delivery_at comparable and present",
                 "threshold_minutes": 0, "comparison": "strictly greater than",
                 "rate": _rate(int(late.sum()), denominator),
                 "excluded_delivered_orders": delivered_count - denominator,
                 "excluded_non_delivered_orders": len(frame) - delivered_count,
                 "limitations": ["Refund treatment and KPI owner unresolved", "Outcome-file labels not used",
                                 "Naive source timestamps have no assigned timezone"]}
    support = {"metric_name": "support_lateness_variant", "business_question": "How often are deliveries more than ten minutes late?",
               "definition_status": "unapproved denominator", "numerator": int(late_10.sum()),
               "denominator": denominator, "eligibility": candidate["eligibility"],
               "threshold_minutes": 10, "comparison": "strictly greater than",
               "rate": _rate(int(late_10.sum()), denominator),
               "excluded_delivered_orders": delivered_count - denominator,
               "limitations": ["Support threshold is documented, but denominator is not stakeholder-approved"]}
    segments = {}
    for label, start, end, duration in (
        ("creation_to_pickup", "created_at", "pickup_at", "creation_to_pickup_min"),
        ("pickup_to_delivery", "pickup_at", "actual_delivery_at", "pickup_to_delivery_min")):
        valid = frame[duration].notna()
        invalid = frame[label + "_invalid_chronology"]
        segments[label] = {"eligible_orders": int(valid.sum()), "missing_milestone_orders": int((frame[start].isna() | frame[end].isna()).sum()),
                           "invalid_chronology_exclusions": int(invalid.sum()),
                           "other_uncomparable_exclusions": int((~valid & ~invalid & frame[start].notna() & frame[end].notna()).sum()),
                           "mean_minutes": float(frame.loc[valid, duration].mean()) if valid.any() else None,
                           "median_minutes": float(frame.loc[valid, duration].median()) if valid.any() else None}
        # Compare observed segment time among the same LDR-eligible delivered population.
        for group_name, mask in (("late", late), ("on_time_or_early", eligible & ~late)):
            segment_group = valid & mask
            segments[label][group_name] = {
                "eligible_orders": int(segment_group.sum()),
                "mean_minutes": float(frame.loc[segment_group, duration].mean()) if segment_group.any() else None,
                "median_minutes": float(frame.loc[segment_group, duration].median()) if segment_group.any() else None,
            }

    lifecycle = {"metric_name": "observed_lifecycle_durations", "business_question": "In which observed segment does time accumulate?",
                 "segments": segments, "limitations": ["No driver-arrived-at-restaurant milestone exists",
                                                  "Durations do not allocate causal delay"]}
    intervention_group = frame.intervention_count.gt(0).map({True: "recorded_intervention", False: "none_recorded"})
    timing = {"before_delivery": 0, "at_or_after_delivery": 0,
              "delivery_time_missing": 0, "intervention_time_missing": 0}
    for row in frame.loc[frame.intervention_count.gt(0)].itertuples(index=False):
        first = row.intervention_first_at
        actual = row.actual_delivery_at
        if pd.isna(first): timing["intervention_time_missing"] += 1
        elif pd.isna(actual): timing["delivery_time_missing"] += 1
        else:
            try: timing["before_delivery" if first < actual else "at_or_after_delivery"] += 1
            except TypeError: timing["intervention_time_missing"] += 1
    intervention = {"metric_name": "intervention_lateness_association", "business_question": "Are recorded interventions associated with different lateness?",
                    "eligibility": candidate["eligibility"], "groups": _late_group(frame, eligible, late, intervention_group),
                    "first_intervention_timing": timing,
                    "limitations": ["Association only; interventions may respond to predicted lateness",
                                    "Timing uses the first recorded intervention and observed delivery"]}
    traffic = frame.traffic_bucket.astype("string").str.strip().str.lower().fillna("unknown/missing").astype(str)
    segmentation = {"metric_name": "traffic_lateness_segmentation", "business_question": "How does observed lateness vary by recorded traffic?",
                    "eligibility": candidate["eligibility"], "groups": _late_group(frame, eligible, late, traffic),
                    "unknown_traffic_orders": int(frame.traffic_bucket.isna().sum()),
                    "limitations": ["Unknown/missing is not an observed traffic category",
                                    "Three duplicate order pairs have unresolved traffic classifications"]}
    return {"population_orders": int(len(frame)), "candidate_operational_ldr": candidate,
            "support_lateness_variant": support, "observed_lifecycle_durations": lifecycle,
            "intervention_lateness_association": intervention,
            "traffic_lateness_segmentation": segmentation}
