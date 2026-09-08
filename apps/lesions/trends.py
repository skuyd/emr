"""Plot only current measurements; preserve every raw value in the table."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext

from .comparison import compare_measurements, measurement_points


AXIS_LABELS = {"LONG": "长径", "SHORT": "短径", "DIAMETER": "直径", "WIDTH": "宽度", "HEIGHT": "高度",
               "DEPTH": "深度", "AP": "前后径", "TRANSVERSE": "横径", "CRANIOCAUDAL": "头尾径"}


def _key(point):
    return point.field_id, point.component_index


def _order(point):
    return not point.date_reliable, point.date_value or "", point.report_id, point.field_id, point.component_index


def _coordinates(points):
    if not points:
        return {}, None, None
    days = [date.fromisoformat(point.date_value).toordinal() for point in points]
    earliest, latest = min(days), max(days)
    low, high = min(point.value for point in points), max(point.value for point in points)
    positions = {}
    with localcontext() as context:
        # Drawing positions may round. The table and arithmetic values do not.
        context.prec = max(40, max(high.adjusted(), low.adjusted(), 0)
                           - min(high.as_tuple().exponent, low.as_tuple().exponent, 0) + 2)
        for point, day in zip(points, days):
            x = Decimal(50) + (Decimal(day - earliest) / (latest - earliest) * 720 if latest != earliest else 360)
            y = Decimal(205) - ((point.value - low) / (high - low) * 170 if high != low else 85)
            positions[_key(point)] = {"point": point, "x": format(x, ".2f"), "y": format(y, ".2f")}
    return positions, low, high


def _chart(kind, axis, unit, ordinal, group, barriers):
    ordered = sorted(group, key=_order)
    visible = [point for point in ordered if point.current_plot_eligible]
    positions, low, high = _coordinates(visible)
    multiplicity = Counter((point.date_value, point.lesion_id) for point in ordered
                           if point.date_reliable and point.role == "CURRENT")
    changes, segments = [], []
    for previous, current in zip(ordered, ordered[1:]):
        comparison = compare_measurements(previous, current)
        reasons = list(comparison["reasons"]) + sorted(barriers)
        if any(multiplicity[(point.date_value, point.lesion_id)] > 1 for point in (previous, current)):
            reasons.append("ambiguous_measurement")
        reasons = tuple(dict.fromkeys(reasons))
        changed = {"previous": previous, "current": current, "comparable": not reasons, "reasons": reasons,
                   "delta": comparison["delta"] if not reasons else None}
        changes.append(changed)
        if not reasons and _key(previous) in positions and _key(current) in positions:
            first, second = positions[_key(previous)], positions[_key(current)]
            segments.append({"x1": first["x"], "y1": first["y"], "x2": second["x"], "y2": second["y"],
                             "previous_field_id": previous.field_id, "current_field_id": current.field_id,
                             "delta": comparison["delta"]})
    title = "SUVmax" if kind == "SUV" else (AXIS_LABELS.get(axis) or f"第 {ordinal + 1} 个尺寸（轴未明确）")
    return {"kind": kind, "title": title, "unit": unit, "points": list(positions.values()), "segments": segments,
            "low": low, "high": high, "first_date": visible[0].date_value if visible else None,
            "last_date": visible[-1].date_value if visible else None}, changes


def build_trends(observations, *, report_maximum=False):
    observations = list(observations)
    measurements = [point for row in observations for point in measurement_points(row)
                    if not report_maximum or point.report_maximum]
    barriers = set()
    if any(not row.get("usable") for row in observations):
        barriers.add("unavailable_observation")
    if any(not point.date_reliable and point.role != "HISTORICAL" for point in measurements):
        barriers.add("date_unreliable")
    groups = defaultdict(list)
    for point in measurements:
        if point.role == "HISTORICAL":
            continue
        # An unnamed axis retains its source ordinal, without assuming it is a
        # long or short diameter or joining it to another report's ordinal.
        groups[(point.kind, point.axis or "", point.unit or "", point.component_index if not point.axis else 0)].append(point)
    charts, comparisons = [], []
    for (kind, axis, unit, ordinal), group in sorted(groups.items()):
        chart, changes = _chart(kind, axis, unit, ordinal, group, barriers)
        chart["id"] = f"lesion-chart-{len(charts) + 1}"
        charts.append(chart)
        comparisons.extend(changes)
    return {"measurements": sorted(measurements, key=_order), "charts": charts,
            "comparisons": comparisons, "limitations": sorted(barriers)}
