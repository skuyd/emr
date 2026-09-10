from decimal import Decimal

from apps.lesions.comparison import compare_measurements, measurement_points
from apps.lesions.trends import build_trends
from .test_comparison import observation


def test_plot_keeps_original_conversion_and_separate_suv_axis_with_exact_dimension_change():
    suv = {"values": ["3.2"], "comparator": "EQ", "unit": None, "approximate": False,
           "measurement_role": "CURRENT", "raw": "SUVmax3.2"}
    rows = [observation(suv=suv), observation(day="2026-09-01", identity="two", suv={**suv, "values": ["4.1"], "raw": "SUVmax4.1"},
             components=[{"value": "15", "unit": "mm", "axis": "LONG"}])]
    result = build_trends(rows)
    assert len(result["measurements"]) == 4
    assert {chart["kind"] for chart in result["charts"]} == {"DIMENSION", "SUV"}
    assert all(len(chart["points"]) == 2 and len(chart["segments"]) == 1 for chart in result["charts"])
    dimension = next(row for row in result["comparisons"] if row["current"].kind == "DIMENSION")
    assert dimension["delta"] == Decimal("3") and dimension["previous"].conversion["factor"] == "10"
    assert dimension["previous"].raw_value == "1.2" and dimension["previous"].raw_unit == "cm"


def test_method_change_unknown_axis_and_conflicting_same_report_values_do_not_create_false_lines():
    first = observation()
    middle = observation(day="2026-08-15", identity="two", method={"code": "MR", "raw": "MR平扫"})
    last = observation(day="2026-09-01", identity="three")
    result = build_trends([first, middle, last])
    assert len(result["charts"]) == 1 and len(result["charts"][0]["points"]) == 3
    assert result["charts"][0]["segments"] == []
    assert all("method_changed" in row["reasons"] for row in result["comparisons"])
    for row in (first, middle, last):
        row["fields"][0]["content"]["value"]["components"][0]["axis"] = None
    unknown = build_trends([first, middle, last])
    assert len(unknown["charts"][0]["points"]) == 3 and not unknown["charts"][0]["segments"]
    assert all("axis_unknown" in row["reasons"] for row in unknown["comparisons"])


def test_duplicate_axis_on_same_exam_day_does_not_pick_last_value_to_join_next_report():
    first = observation()
    duplicate = observation(identity="two", day="2026-08-01",
                             components=[{"value": "18", "unit": "mm", "axis": "LONG"}])
    last = observation(day="2026-09-01", identity="three")
    result = build_trends([first, duplicate, last])
    assert len(result["measurements"]) == 3 and len(result["charts"][0]["points"]) == 3
    assert result["charts"][0]["segments"] == []
    assert all("ambiguous_measurement" in row["reasons"] for row in result["comparisons"])


def test_stale_or_undated_intervening_observation_retains_points_but_breaks_unjustified_lines():
    first, middle, last = observation(), observation(day="2026-08-15", identity="two"), observation(day="2026-09-01", identity="three")
    middle["usable"] = False
    result = build_trends([first, middle, last])
    assert len(result["measurements"]) == 2 and len(result["charts"][0]["points"]) == 2
    assert not result["charts"][0]["segments"] and "unavailable_observation" in result["limitations"]
    middle["usable"] = True
    middle["date"] = {"value": None, "precision": "UNKNOWN"}
    result = build_trends([first, middle, last])
    assert len(result["measurements"]) == 3 and len(result["charts"][0]["points"]) == 2
    assert not result["charts"][0]["segments"] and "date_unreliable" in result["limitations"]


def test_report_maximum_view_uses_only_explicit_qualifiers_and_breaks_when_stable_identity_changes():
    rows = [observation(maximum="REPORT_MAXIMUM"),
            observation(day="2026-08-15", identity="two", maximum="GROUP_LARGER",
                        components=[{"value": "999", "unit": "mm", "axis": "LONG"}]),
            observation(day="2026-09-01", identity="three", maximum="REPORT_MAXIMUM")]
    rows[-1]["lesion_id"] = "lesion-2"
    result = build_trends(rows, report_maximum=True)
    assert len(result["measurements"]) == 2 and all(row.report_maximum for row in result["measurements"])
    assert all(row.raw_value != "999" for row in result["measurements"])
    assert not result["charts"][0]["segments"] and "lesion_changed" in result["comparisons"][0]["reasons"]


def test_metric_conversion_and_difference_do_not_round_high_precision_reported_digits():
    raw = "1234567890123456789012345678.123456789"
    before = measurement_points(observation(components=[{"value": raw, "unit": "cm", "axis": "LONG"}]))[0]
    after = measurement_points(observation(day="2026-09-01", identity="two",
                 components=[{"value": "12345678901234567890123456782.23456789", "unit": "mm", "axis": "LONG"}]))[0]
    assert before.value == Decimal("12345678901234567890123456781.23456789")
    assert compare_measurements(before, after)["delta"] == Decimal("1")
