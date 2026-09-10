from copy import deepcopy
from decimal import Decimal

from apps.lesions.comparison import compare_measurements, measurement_points


def observation(*, day="2026-08-01", method=None, identity="observation-1", role="CURRENT",
                components=None, suv=None, maximum=None, conflict=False):
    if components is None:
        components = [{"value": "1.2", "unit": "cm", "axis": "LONG"}]
    fields = [{"id": "dimension-" + identity, "field_key": "lesion.dimensions", "usable": True,
               "conflict": conflict, "content": {"value": {
                   "components": components, "approximate": True,
                   "measurement_role": role, "raw": "约1.2cm",
               }}}]
    if suv is not None:
        fields.append({"id": "suv-" + identity, "field_key": "lesion.suvmax", "usable": True,
                       "conflict": False, "content": {"value": suv}})
    if maximum is not None:
        fields.append({"id": "maximum-" + identity, "field_key": "lesion.maximum_scope", "usable": True,
                       "conflict": False, "content": {"value": {"code": maximum, "raw": "合成限定"}}})
    return {"id": identity, "lesion_id": "lesion-1", "report_id": "report-" + identity,
            "usable": True, "date": {"value": day, "precision": "DAY"},
            "method": method or {"code": "CT", "raw": "CT平扫"}, "fields": fields}


def test_dimension_conversion_preserves_raw_order_and_explains_comparable_change():
    earlier = observation()
    later = observation(day="2026-09-01", identity="observation-2",
                        components=[{"value": "15", "unit": "mm", "axis": "LONG"}])
    original = deepcopy(earlier)
    before, after = measurement_points(earlier), measurement_points(later)
    assert len(before) == len(after) == 1
    point = before[0]
    assert (point.raw_value, point.raw_unit, point.value, point.unit) == ("1.2", "cm", Decimal("12"), "mm")
    assert point.conversion == {"factor": "10", "from": "cm", "to": "mm", "rule": "metric_length_cm_to_mm_v1"}
    result = compare_measurements(point, after[0])
    assert result == {"comparable": True, "reasons": (), "delta": Decimal("3")}
    assert earlier == original


def test_unknown_axes_keep_three_ordered_values_and_never_invent_a_long_axis_line():
    earlier = observation(components=[{"value": value, "unit": "mm", "axis": None} for value in ("9", "12", "8")])
    later = observation(day="2026-09-01", identity="observation-2")
    points = measurement_points(earlier)
    assert len(points) == 3
    assert [point.raw_value for point in points] == ["9", "12", "8"]
    assert [point.component_index for point in points] == [0, 1, 2]
    assert all(point.current_plot_eligible for point in points)
    result = compare_measurements(points[0], measurement_points(later)[0])
    assert not result["comparable"] and result["delta"] is None
    assert "axis_unknown" in result["reasons"]


def test_recorded_method_change_keeps_values_without_computing_a_change():
    before = measurement_points(observation())
    after = measurement_points(observation(day="2026-09-01", identity="observation-2",
                                           method={"code": "MR", "raw": "磁共振增强"}))
    assert len(before) == len(after) == 1
    result = compare_measurements(before[0], after[0])
    assert not result["comparable"] and result["delta"] is None
    assert "method_changed" in result["reasons"]


def test_historical_suv_and_bounded_value_do_not_become_duplicate_current_points():
    history = {"values": ["5.4"], "comparator": "EQ", "unit": None, "approximate": False,
               "measurement_role": "HISTORICAL", "raw": "前次SUVmax5.4"}
    interval = {"values": ["3.2", "4.1"], "comparator": "RANGE", "unit": None, "approximate": True,
                "measurement_role": "CURRENT", "raw": "本次SUVmax约3.2-4.1"}
    earlier = measurement_points(observation(suv=history))
    later = measurement_points(observation(day="2026-09-01", identity="observation-2", suv=interval))
    assert len(earlier) == len(later) == 2
    before = next(point for point in earlier if point.kind == "SUV")
    after = next(point for point in later if point.kind == "SUV")
    assert not before.current_plot_eligible
    assert before.raw_expression == "前次SUVmax5.4"
    assert after.value is None and after.range_values == (Decimal("3.2"), Decimal("4.1"))
    assert after.raw_expression == "本次SUVmax约3.2-4.1"
    result = compare_measurements(before, after)
    assert not result["comparable"] and result["delta"] is None
    assert {"historical_or_unknown_role", "bounded_value"} <= set(result["reasons"])


def test_only_explicit_report_maximum_can_enter_the_maximum_view():
    group = measurement_points(observation(maximum="GROUP_LARGER"))
    report = measurement_points(observation(maximum="REPORT_MAXIMUM"))
    assert len(group) == len(report) == 1
    assert not group[0].report_maximum and report[0].report_maximum
    source = observation(maximum="REPORT_MAXIMUM")
    source["fields"][-1]["usable"] = False
    assert not measurement_points(source)[0].report_maximum


def test_conflicts_uncertain_dates_and_stale_assignment_cannot_form_a_trend_line():
    earlier = observation(conflict=True)
    earlier["date"] = {"value": "2026-08", "precision": "MONTH"}
    points = measurement_points(earlier)
    assert len(points) == 1
    result = compare_measurements(points[0], measurement_points(observation(day="2026-09-01", identity="observation-2"))[0])
    assert not result["comparable"] and result["delta"] is None
    assert {"field_conflict", "date_unreliable"} <= set(result["reasons"])
    earlier["usable"] = False
    assert measurement_points(earlier) == []
