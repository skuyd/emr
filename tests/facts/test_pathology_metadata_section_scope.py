"""Patient specimen anchors must preserve their governing printed section."""
from copy import deepcopy

import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract


def section_metadata_rows(heading, *, style="split", above_title=False, identity="SYN-QC-BLOCK"):
    items = [("免疫组化检测报告单", (.2, .40 if above_title else .04, .8, .43 if above_title else .07))]
    if heading:
        items.append((heading, (.05, .10, .95, .13)))
    metadata = [("肿瘤样本编号", identity), ("检测项目", "SYN-QC-ASSAY"), ("报告日期", "2037-02-03")]
    for i, (label, value) in enumerate(metadata):
        y = .18 + i * .06
        if style == "split":
            items.extend([(label, (.05, y, .25, y + .03)), (value, (.35, y, .65, y + .03))])
        else:
            items.append((label + "：" + value, (.05, y, .95, y + .03)))
    items += [("检测结果", (.05, .47, .95, .50)), ("PD-L1 TPS：13%", (.05, .68, .95, .71))]
    rows = [block(text, order=i, box=box) for i, (text, box) in enumerate(items)]
    if style == "multiline":
        return [block("\n".join(text for text, _ in sorted(items, key=lambda item: item[1][1])), order=0)]
    return rows


def assert_score_has_no_excluded_anchor(fields):
    score = next(f for f in fields if f.key == "ihc.score")
    assert score.value["values"] == ["13"] and score.source_role == "CURRENT_RESULT"
    nodes = {f.node_id: f for f in fields}
    for role in ("SPECIMEN", "ASSAY"):
        target = nodes.get(score.links[role])
        assert target is None or "SYN-QC-" not in target.raw_value
    assert not [f for f in fields if f.key == "assay.report_date"]


@pytest.mark.parametrize("heading", ["质量控制", "样本质控结果", "阳性对照", "阴性对照", "检测说明", "检测图谱", "备注"])
@pytest.mark.parametrize("style", ["split", "inline", "multiline"])
def test_excluded_metadata_cannot_anchor_later_current_score(heading, style):
    rows = section_metadata_rows(heading, style=style)
    before = deepcopy([(row.text, row.polygon) for row in rows])
    reports, unknown, groups = extract(rows)
    assert len(reports) == 1 and not unknown
    assert_score_has_no_excluded_anchor(groups[0])
    assert [(row.text, row.polygon) for row in rows] == before


@pytest.mark.parametrize("heading", ["质量控制", "阳性对照", "检测说明"])
def test_excluded_section_before_title_does_not_lose_its_role(heading):
    _, _, groups = extract(section_metadata_rows(heading, above_title=True))
    assert_score_has_no_excluded_anchor(groups[0])


@pytest.mark.parametrize("heading", [None, "送检信息", "受检者基本信息", "样本基本信息"])
@pytest.mark.parametrize("above_title", [False, True])
def test_real_patient_metadata_is_retained_before_and_after_title(heading, above_title):
    _, _, groups = extract(section_metadata_rows(heading, above_title=above_title, identity="SYN-PATIENT"))
    fields = groups[0]
    by_id = {f.node_id: f for f in fields}
    score = next(f for f in fields if f.key == "ihc.score")
    assert by_id[score.links["SPECIMEN"]].value["raw"] == "SYN-PATIENT"
    assert by_id[score.links["ASSAY"]].value["raw"] == "SYN-QC-ASSAY"
    assert next(f for f in fields if f.key == "assay.report_date").value["value"] == "2037-02-03"


def test_new_current_section_can_supply_its_own_patient_anchors():
    rows = section_metadata_rows("质量控制")
    for index, (label, value) in enumerate([("标本编号", "SYN-PATIENT"), ("检测项目", "SYN-PATIENT-ASSAY")]):
        y = .54 + index * .06
        rows += [block(label, order=30 + index * 2, box=(.05, y, .25, y + .025)),
                 block(value, order=31 + index * 2, box=(.35, y, .75, y + .025))]
    _, _, groups = extract(rows)
    fields = groups[0]
    by_id = {f.node_id: f for f in fields}
    score = next(f for f in fields if f.key == "ihc.score")
    assert by_id[score.links["SPECIMEN"]].value["raw"] == "SYN-PATIENT"
    assert by_id[score.links["ASSAY"]].value["raw"] == "SYN-PATIENT-ASSAY"
    assert not [f for f in fields if f.key == "assay.report_date"]


@pytest.mark.parametrize("layout", ["serial", "parallel"])
def test_metadata_section_does_not_cross_another_report(layout):
    rows = []
    for panel, heading in enumerate(["质量控制", None]):
        part = section_metadata_rows(heading, identity="SYN-QC-BLOCK" if panel == 0 else "SYN-PATIENT")
        for row in part:
            if layout == "serial":
                row.polygon = [[x, y * .45 + panel * .5] for x, y in row.polygon]
            else:
                row.polygon = [[x * .45 + panel * .55, y] for x, y in row.polygon]
            row.reading_order += len(rows)
        rows.extend(part)
    reports, _, groups = extract(rows)
    assert len(reports) == 2
    assert_score_has_no_excluded_anchor(groups[0])
    score = next(f for f in groups[1] if f.key == "ihc.score")
    by_id = {f.node_id:f for f in groups[1]}
    assert by_id[score.links["SPECIMEN"]].value["raw"] == "SYN-PATIENT"


@pytest.mark.django_db
@pytest.mark.parametrize("heading", ["质量控制", "检测说明", None])
def test_persisted_graph_keeps_excluded_metadata_out_of_patient_binding(django_user_model, heading):
    from apps.facts.clinical_context import validate_context_candidate
    from apps.facts.models import Fact
    from tests.facts.test_pathology_pipeline import fixture

    _, _, document, _, _ = fixture(django_user_model, name="section-metadata-" + str(heading), rows=section_metadata_rows(heading))
    score = document.facts.get(field_key="ihc.score")
    validate_context_candidate(score)
    for binding in score.automatic_content["entity_context"]["bindings"]:
        if binding["role"] in {"SPECIMEN", "ASSAY"}:
            if heading:
                assert binding["state"] == "UNKNOWN" and binding["target_fact_id"] is None
            else:
                assert binding["state"] == "BOUND"
                assert "SYN-QC-" in Fact.objects.get(pk=binding["target_fact_id"]).automatic_content["raw_value"]
