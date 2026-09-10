from collections import Counter
from dataclasses import dataclass

import pytest

from apps.cancer_ordering.profiles import prioritize, prioritize_groups
from apps.labs.comparison import ComparisonGroup


@dataclass(frozen=True)
class Row:
    id: str
    standard_code: str
    raw_value: str = "未检出"
    unit: str = ""
    specimen: str = "未知"


def test_lung_order_preserves_each_heterogeneous_and_repeated_item():
    cea = Row("cea-serum", "LAB_CEA", "8.6", "ng/mL", "血清")
    values = [Row("unknown", "UNMAPPED"), Row("nse", "LAB_NSE"), cea,
              Row("platelets", "LAB_PLT", "86", "10^9/L", "全血"),
              Row("cea-other", "LAB_CEA", "<5", "ug/L", "其他"),
              Row("progrp", "LAB_PROGRP"), Row("cyfra", "LAB_CYFRA21_1"), cea]
    original = tuple(values)
    result = prioritize(values, "LUNG")
    assert [item.id for item in result] == [
        "cea-serum", "cea-other", "cea-serum", "cyfra", "nse", "progrp", "unknown", "platelets",
    ]
    assert Counter(map(id, result)) == Counter(map(id, original))
    assert tuple(values) == original
    assert result[1].raw_value == "<5" and result[1].unit == "ug/L" and result[1].specimen == "其他"


def test_pancreas_order_does_not_adopt_other_profiles_or_drop_unknown_codes():
    values = [Row("nse", "LAB_NSE"), Row("cea", "LAB_CEA"),
              Row("unknown", "lab_cea"), Row("ca19", "LAB_CA19_9"), Row("progrp", "LAB_PROGRP")]
    assert [row.id for row in prioritize(values, "PANCREAS")] == ["ca19", "cea", "nse", "unknown", "progrp"]


def test_general_and_empty_results_preserve_existing_order():
    values = [Row("b", "LAB_NSE"), Row("a", "LAB_CEA"), Row("c", "UNMAPPED")]
    assert prioritize(values, "GENERAL") == tuple(values)
    assert prioritize([], "LUNG") == ()


def test_grouped_comparison_promotes_the_visible_group_and_preserves_every_row():
    blood = ComparisonGroup("CBC", (Row("plt", "LAB_PLT"),))
    tumor = ComparisonGroup("TUMOR_MARKER", (Row("nse", "LAB_NSE"), Row("cea", "LAB_CEA")))
    unknown = ComparisonGroup("未归类", (Row("raw", "RAW"),))
    empty = ComparisonGroup("EMPTY", ())
    result = prioritize_groups((blood, empty, unknown, tumor), "LUNG")
    assert [group.category for group in result] == ["TUMOR_MARKER", "CBC", "EMPTY", "未归类"]
    assert [row.id for row in result[0].rows] == ["cea", "nse"]
    assert result[1] is blood and result[2] is empty and result[3] is unknown
    assert tumor.rows[0].id == "nse"
    assert prioritize_groups((blood, empty, unknown, tumor), "GENERAL") == (blood, empty, unknown, tumor)


def test_selected_output_can_supply_its_own_code_accessor():
    values = [{"code": "LAB_NSE", "id": "n"}, {"code": "LAB_CEA", "id": "c"}, {"code": "RAW", "id": "r"}]
    assert [row["id"] for row in prioritize(values, "LUNG", code_of=lambda row: row["code"])] == ["c", "n", "r"]


def test_invalid_profile_is_rejected_instead_of_silently_using_an_unreviewed_mapping():
    with pytest.raises(ValueError):
        prioritize([Row("cea", "LAB_CEA")], "made-up-profile")
