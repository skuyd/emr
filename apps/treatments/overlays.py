"""Observed points and explicit key-node choices; no clinical phase inference."""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from apps.labs.numerics import calculate_numeric

from .signals import digest
from .timeline import exact_day


METRICS = ("ANC", "PLT", "HGB")
METRIC_CODES = {"ANC": "LAB_NEUT_COUNT", "PLT": "LAB_PLT", "HGB": "LAB_HGB"}
NODE_LABELS = {"PRE_ANCHOR": "锚点前最近可比日", "OBSERVED_MIN": "周期内实际最低值", "ASSESSMENT": "明确评估节点", "LATEST": "最近可验证记录"}


def _numeric(row):
    if not row.get("trend_eligible") or not row.get("source_valid") or not exact_day(row.get("date"), row.get("date_precision")):
        return None
    try:
        value = Decimal(str(row.get("numeric_value")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _segments(points):
    counts = Counter(point["relative_day"] for point in points)
    segments, current = [], []
    for point in points:
        if counts[point["relative_day"]] > 1:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(point["id"])
    if current:
        segments.append(current)
    return segments


def build_cycle_overlays(timeline, material, selection=None):
    selection = selection or {}
    metrics = {METRIC_CODES[key] for key in selection.get("cycle_metric_codes", METRICS) if key in METRIC_CODES}
    records = [row for row in material.get("records", []) if row["kind"] == "observation" and row.get("standard_code") in metrics]
    eligible = [row for row in records if _numeric(row) is not None]
    links = {(row["kind"], row["source_id"]): row for row in timeline["links"]}
    unplottable = [{"source_id": row["id"], "document_id": row["document_id"], "label": row.get("label", ""),
                   "url": row.get("url", ""), "date": row.get("date"), "raw_value": row.get("raw_value", row.get("numeric_value")),
                   "issues": deepcopy(row.get("quality_issues", [])), "reason": "comparison_not_eligible"}
                  for row in records if _numeric(row) is None]
    points, nodes, missing, series = [], [], [], []
    by_group = defaultdict(list)
    for row in eligible:
        by_group[tuple(row["group_key"])].append(row)
    for cycle in timeline["cycles"]:
        anchor = exact_day(cycle["content"].get("anchor"), cycle["content"].get("anchor_precision"))
        if anchor is None:
            missing.append({"cycle_id": cycle["id"], "kind": "ANCHOR", "reason": "anchor_not_exact"})
            continue
        own_events = {row["event_id"] for row in cycle.get("event_links", [])}
        assessments = defaultdict(list)
        for event in material.get("events", []):
            content = event["content"]
            if (event["id"] in own_events and event.get("source_valid") and content.get("kind") == "ASSESSMENT"
                    and content.get("occurrence") == "OCCURRED" and (event.get("usable") or cycle["preview"])
                    and exact_day(content.get("date"), content.get("date_precision"))):
                assessments[content["date"]].append(event["id"])
        for key, group in sorted(by_group.items()):
            own = [row for row in group if links.get(("observation", row["id"]), {}).get("cycle_id") == cycle["id"]]
            prior = [row for row in group if row["date"] < anchor.isoformat()]
            prior_day = max(row["date"] for row in prior) if prior else None
            pre = [row for row in prior if row["date"] == prior_day]
            selected = {row["id"]: row for row in [*pre, *own]}
            if not selected:
                continue
            after = [row for row in own if row["date"] >= anchor.isoformat()]
            low = min(_numeric(row) for row in after) if after else None
            latest = max(row["date"] for row in after) if after else None
            if not pre:
                missing.append({"cycle_id": cycle["id"], "group_key": key, "kind": "PRE_ANCHOR", "reason": "no_comparable_prior_day"})
            if not after:
                missing.extend({"cycle_id": cycle["id"], "group_key": key, "kind": kind, "reason": "no_comparable_cycle_points"}
                               for kind in ["OBSERVED_MIN", "LATEST"])
            if not any(row["date"] in assessments for row in own):
                missing.append({"cycle_id": cycle["id"], "group_key": key, "kind": "ASSESSMENT", "reason": "no_explicit_assessment_measurement"})
            current = []
            for row in sorted(selected.values(), key=lambda item: (item["date"], item["id"])):
                labels = []
                if row in pre:
                    labels.append("PRE_ANCHOR")
                if row in after and _numeric(row) == low:
                    labels.append("OBSERVED_MIN")
                if row in own and row["date"] in assessments:
                    labels.append("ASSESSMENT")
                if row in after and row["date"] == latest:
                    labels.append("LATEST")
                point = {"id": digest({"cycle": cycle["id"], "observation": row["id"]}), "cycle_id": cycle["id"],
                         "regimen_id": cycle.get("regimen_id"), "observation_id": row["id"], "document_id": row["document_id"],
                         "date": row["date"], "relative_day": (exact_day(row["date"], "DAY") - anchor).days,
                         "value": str(_numeric(row)), "raw_value": row.get("raw_value", row["numeric_value"]), "unit": row.get("unit", ""),
                         "standard_code": row["standard_code"], "group_key": key, "source_token": row.get("source_token"),
                         "url": row.get("url", ""), "labels": labels, "label_text": "、".join(NODE_LABELS[label] for label in labels),
                         "preview": cycle["preview"], "context_only": row["id"] not in {item["id"] for item in own}}
                current.append(point)
                for label in labels:
                    nodes.append({"id": digest({"point": point["id"], "kind": label}), "point_id": point["id"],
                                  "cycle_id": cycle["id"], "observation_id": row["id"], "kind": label, "label": NODE_LABELS[label],
                                  "event_ids": sorted(assessments[row["date"]]) if label == "ASSESSMENT" else []})
            points.extend(current)
            series.append({"id": digest({"cycle": cycle["id"], "group": key}), "cycle_id": cycle["id"],
                           "anchor": cycle["content"].get("anchor"), "ordinal": cycle["content"].get("ordinal"),
                           "regimen_id": cycle.get("regimen_id"), "group_key": key, "unit": group[0].get("unit", ""),
                           "points": current, "segments": _segments(current), "preview": cycle["preview"]})
    reasons = {"anchor_not_exact": "锚点未精确到日", "no_comparable_prior_day": "没有锚点前的可比日记录",
               "no_comparable_cycle_points": "没有此周期内的可比检验点", "no_explicit_assessment_measurement": "没有来源明确关联的评估检验"}
    for row in missing:
        row["label"] = NODE_LABELS.get(row["kind"], "周期锚点")
        row["reason_label"] = reasons[row["reason"]]
    return {"points": points, "key_nodes": nodes, "missing_nodes": missing, "series": series, "unplottable": unplottable}


def chart_groups(overlays, *, mode="key"):
    """SVG coordinates only. Full precision source points remain in overlays."""
    grouped = defaultdict(list)
    for series in overlays["series"]:
        grouped[(series["regimen_id"], series["group_key"])].append(series)
    result = []
    for (regimen_id, key), series in grouped.items():
        points = [point for row in series for point in row["points"] if mode == "full" or point["labels"]]
        if not points:
            continue
        left, right = min(p["relative_day"] for p in points), max(p["relative_day"] for p in points)
        low, high = min(Decimal(p["value"]) for p in points), max(Decimal(p["value"]) for p in points)
        positions = {}
        for point in points:
            x = 48 + ((point["relative_day"] - left) / (right - left) * 656 if right != left else 328)
            ratio = calculate_numeric(lambda: (Decimal(point["value"]) - low) / (high - low)) if high != low else Decimal("0.5")
            y = 242 - float(ratio if ratio is not None else Decimal("0.5")) * 208
            positions[point["id"]] = {**deepcopy(point), "x": round(x, 2), "y": round(y, 2)}
        displayed = []
        for index, row in enumerate(series):
            visible = [positions[p["id"]] for p in row["points"] if p["id"] in positions]
            paths = []
            # Preserve full-data tie breaks even when tied nodes are hidden.
            for segment in row["segments"]:
                selected = [positions[identity] for identity in segment if identity in positions]
                if len(selected) > 1:
                    paths.append(" ".join(f'{p["x"]},{p["y"]}' for p in selected))
            displayed.append({**row, "points": visible, "paths": paths, "color_index": index % 6})
        result.append({"id": digest({"regimen": regimen_id, "key": key}), "regimen_id": regimen_id, "group_key": key,
                       "standard_code": key[0], "basis_label": " · ".join(key[1:4]), "unit": series[0]["unit"],
                       "series": displayed, "left": left, "right": right, "low": str(low), "high": str(high),
                       "zero_x": 48 + (0 - left) / (right - left) * 656 if left <= 0 <= right and right != left else None})
    return result
