"""Independent review regressions through actual original OCR persistence."""
from copy import deepcopy

import pytest

from apps.facts.models import FactRevision
from apps.facts.readmodels import effective_fact
from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_pipeline import fixture
from tests.facts.test_pathology_tables import table_rows as single_marker_table


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(("text", "assertion"), [
    ("未检测到表达", "NOT_DETECTED"), ("未检测 到表达", "NOT_DETECTED"),
    ("未检出表达", "NOT_DETECTED"), ("未检测", "NOT_TESTED"), ("未做检测", "NOT_TESTED"),
    ("未行检测", "NOT_TESTED"), ("不能排除表达", "UNCERTAIN"), ("阳性", "POSITIVE"),
])
def test_actual_result_preserves_not_detected_versus_not_tested(django_user_model, text, assertion):
    rows = [block(line, order=index, box=(.05, .05 + index * .08, .95, .08 + index * .08)) for index, line in enumerate([
        "病理诊断报告书", "标本编号：SYN-REVIEW", "检测项目：PD-L1免疫组化", "检测结果：PD-L1 " + text,
    ])]
    _, _, document, version, run = fixture(django_user_model, rows=rows, name="parser-assertion-closure")
    result = document.facts.get(field_key="ihc.result")
    assert run.status == "EXTRACTED"
    assert result.automatic_content["value"] == {"text": text, "assertion": assertion}
    assert result.revision_number == 0 and not effective_fact(result)["usable"]
    assert not FactRevision.objects.filter(fact__document=document).exists()
    assert list(version.ocr_blocks.order_by("reading_order").values_list("text", flat=True)) == [row.text for row in rows]


def two_marker_rows(second_marker, *, overlapping=False, separate_layout=False):
    items = [
        ("免疫组化检测报告单", (.2, .04, .8, .08)), ("标本编号：SYN-TABLE", (.05, .12, .5, .15)),
        ("检测项目：合成免疫组化", (.05, .19, .6, .22)), ("抗体名称", (.05, .3, .22, .33)),
        ("检测方法", (.4, .3, .6, .33)), ("检测结果", (.74, .3, .96, .33)),
        ("PD-L1", (.05, .4, .22, .43)), ("IHC", (.4, .4, .6, .43)), ("CPS：11", (.74, .4, .96, .43)),
        (second_marker, (.05, .5, .22, .53)), ("IHC", (.4, .5, .6, .53)),
        ("TPS：22%", (.74, .4 if overlapping else .5, .96, .53)),
    ]
    rows = [block(text, order=index, box=box) for index, (text, box) in enumerate(items)]
    if separate_layout:
        for row in rows:
            row.layout_polygon = deepcopy(row.polygon)
            row.polygon = [[.2, .1], [.9, .1], [.9, .7], [.2, .7]]
    return rows


@pytest.mark.parametrize("second_marker", ["SYN-MARKER", "Ki-67-AS1", "ER"])
def test_every_marker_cell_limits_result_scope_even_when_its_name_is_unsupported(django_user_model, second_marker):
    rows = two_marker_rows(second_marker)
    _, _, document, version, _ = fixture(django_user_model, rows=rows, name="parser-unknown-marker-closure")
    marker = document.facts.get(field_key="ihc.marker", automatic_content__value__code="PD_L1")
    scores = list(document.facts.filter(field_key="ihc.score", entity_key=marker.entity_key))
    assert [(item.automatic_content["value"]["score_kind"], item.automatic_content["value"]["values"]) for item in scores] == [("CPS", ["11"])]
    if second_marker == "ER":
        second = document.facts.get(field_key="ihc.marker", automatic_content__value__code="ER")
        assert document.facts.get(field_key="ihc.score", entity_key=second.entity_key).automatic_content["value"]["values"] == ["22"]
    else:
        assert document.facts.filter(field_key="ihc.score").count() == 1
    for score in scores:
        assert not effective_fact(score)["usable"] and score.revision_number == 0
        assert all("TPS：22" not in source.raw_text for source in score.source_fragments.all())
    assert list(version.ocr_blocks.order_by("reading_order").values_list("text", flat=True)) == [row.text for row in rows]


@pytest.mark.parametrize("separate_layout", [False, True])
def test_result_box_crossing_known_and_unknown_marker_rows_cannot_pick_known_one(django_user_model, separate_layout):
    rows = two_marker_rows("SYN-MARKER", overlapping=True, separate_layout=separate_layout)
    _, _, document, _, _ = fixture(django_user_model, rows=rows, name="parser-cross-row-closure")
    scores = list(document.facts.filter(field_key="ihc.score"))
    assert [(score.automatic_content["value"]["score_kind"], score.automatic_content["value"]["values"]) for score in scores] == [("CPS", ["11"])]
    if separate_layout:
        assert all(source.polygon == rows[0].polygon for score in scores for source in score.source_fragments.all())


def test_one_actual_marker_with_stacked_scores_remains_supported(django_user_model):
    _, _, document, _, _ = fixture(django_user_model, rows=single_marker_table(), name="parser-single-marker-control")
    scores = list(document.facts.filter(field_key="ihc.score").order_by("reading_order"))
    assert [(score.automatic_content["value"]["score_kind"], score.automatic_content["value"]["values"]) for score in scores] == [("TPS", ["13"]), ("CPS", ["21"])]
