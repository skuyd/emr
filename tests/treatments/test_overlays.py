from tests.treatments.test_timeline import cycle_row, material, record


def point(identity, day, value, *, group=("LAB_NEUT_COUNT", "BLOOD", "10^9/l", "COUNT", "trusted"), eligible=True):
    return record(identity, day, kind="observation", standard_code=group[0], group_key=group, numeric_value=value,
                  unit="10^9/L", trend_eligible=eligible, quality_issues=[] if eligible else [{"code": "uncertain_value"}])


def overlay(records, cycles=None, **kwargs):
    from apps.treatments.timeline import build_cycle_timeline
    from apps.treatments.overlays import build_cycle_overlays
    source = material(cycles or [cycle_row("c", "2024-02-29")], records, **kwargs)
    return build_cycle_overlays(build_cycle_timeline(source), source)


def test_relative_days_use_actual_leap_day_and_keep_pre_anchor_negative_context():
    result = overlay([point("pre", "2024-02-28", "2"), point("anchor", "2024-02-29", "3"), point("after", "2024-03-02", "1")])
    assert {row["observation_id"]: row["relative_day"] for row in result["points"]} == {"pre": -1, "anchor": 0, "after": 2}
    assert next(row for row in result["points"] if row["observation_id"] == "anchor")["labels"] != ["PRE_ANCHOR"]


def test_same_day_results_and_actual_minimum_ties_survive_and_break_lines():
    result = overlay([point("a", "2024-02-29", "2"), point("b", "2024-03-01", "1"), point("c", "2024-03-01", "1"), point("d", "2024-03-03", "3")])
    assert len(result["points"]) == 4
    assert {row["observation_id"] for row in result["key_nodes"] if row["kind"] == "OBSERVED_MIN"} == {"b", "c"}
    assert all(not ({"b", "c"} & set(segment)) for segment in result["series"][0]["segments"])
    assert not any(len(segment) > 1 for segment in result["series"][0]["segments"])


def test_key_mode_keeps_hidden_full_points_and_does_not_assume_d8():
    result = overlay([point("a", "2024-02-29", "4"), point("b", "2024-03-01", "3"), point("c", "2024-03-03", "1"),
                      point("d", "2024-03-07", "2"), point("e", "2024-03-09", "4")])
    assert {row["observation_id"] for row in result["points"]} == {"a", "b", "c", "d", "e"}
    assert {row["observation_id"] for row in result["key_nodes"]} == {"c", "e"}
    assert result["points"][0]["relative_day"] == 0


def test_group_keys_and_regimens_never_connect_unlike_measurements():
    records = [point("a", "2024-02-29", "2"), point("b", "2024-03-01", "3", group=("LAB_NEUT_COUNT", "BLOOD", "10^9/l", "OTHER", "trusted")),
               point("c", "2024-03-22", "4")]
    result = overlay(records, cycles=[cycle_row("one", "2024-02-29"), cycle_row("two", "2024-03-21", regimen="scheme-b")])
    assert len({row["group_key"] for row in result["series"]}) == 2
    assert all(len({point["cycle_id"] for point in row["points"]}) == 1 for row in result["series"])


def test_unplottable_records_remain_visible_and_never_supply_minimum_or_previous():
    result = overlay([point("pre", "2024-02-28", "<1", eligible=False), point("current", "2024-02-29", "3"),
                      point("bad", "2024-03-02", "0", eligible=False)])
    assert [row["observation_id"] for row in result["points"]] == ["current"]
    assert {row["source_id"] for row in result["unplottable"]} == {"pre", "bad"}
    assert any(row["kind"] == "PRE_ANCHOR" and row["reason"] == "no_comparable_prior_day" for row in result["missing_nodes"])


def test_explicit_assessment_source_adds_node_but_not_an_invented_assessment():
    cycle = cycle_row("c", "2024-02-29")
    cycle["event_links"] = [{"event_id": "assessment", "role": "ASSESSMENT"}]
    event = {"id": "assessment", "usable": True, "source_valid": True, "status": "CONFIRMED",
             "content": {"kind": "ASSESSMENT", "occurrence": "OCCURRED", "date": "2024-03-02", "date_precision": "DAY"}}
    result = overlay([point("a", "2024-03-01", "1"), point("b", "2024-03-02", "3"), point("c", "2024-03-04", "4")], cycles=[cycle], events=[event])
    assessments = [row for row in result["key_nodes"] if row["kind"] == "ASSESSMENT"]
    assert len(assessments) == 1 and assessments[0]["observation_id"] == "b" and assessments[0]["event_ids"] == ["assessment"]
