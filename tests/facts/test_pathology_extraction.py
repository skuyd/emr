"""Synthetic original OCR layouts; these examples are independent of real gold."""
from copy import deepcopy

import pytest

from tests.facts.test_clinical_segments import block


def report_rows():
    texts = [
        "合成医院 病理诊断报告书",
        "标本编号：SYN-A 标本类型：蜡块 取材部位：左侧组织",
        "检测项目：PD-L1免疫组化 检测方法：IHC 抗体克隆号：SYN-CLONE",
        "收样日期：2030-04-02 报告日期：2030-04-05 采样日期：/",
        "检测结果：PD-L1 TPS：13% CPS：21",
        "质控：阳性对照TPS：100%；阴性对照CPS：0",
        "说明：TPS超过某阈值可作为研究参考，不是本标本结果。",
    ]
    return [block(text, order=i, box=(.05, .05 + i * .08, .95, .08 + i * .08)) for i, text in enumerate(texts)]


def extract(rows):
    from apps.facts.pathology_extraction import pathology_candidates
    from apps.facts.pathology_segments import segment_pathology_reports

    segments, unknown = segment_pathology_reports(rows)
    return segments, unknown, [pathology_candidates(segment) for segment in segments]


def test_literal_ihc_scores_retain_separate_scale_and_original_unicode_sources():
    rows = report_rows()
    original = [(row.text, deepcopy(row.polygon)) for row in rows]
    segments, unknown, groups = extract(rows)
    assert len(segments) == 1 and unknown == set()
    fields = groups[0]
    scores = [field for field in fields if field.key == "ihc.score"]
    assert [(field.value["score_kind"], field.value["values"], field.value["unit"]) for field in scores] == [
        ("TPS", ["13"], "%"), ("CPS", ["21"], None),
    ]
    assert all(field.value["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED" for field in scores)
    assert scores[0].entity == scores[1].entity
    by_id = {field.node_id: field for field in fields}
    for score in scores:
        assert by_id[score.links["MARKER"]].value["code"] == "PD_L1"
        assert by_id[score.links["ASSAY"]].key == "assay.identity"
        assert by_id[score.links["SPECIMEN"]].value["raw"] == "SYN-A"
    dates = {field.key: field.value for field in fields if field.key.endswith("_date")}
    assert dates == {"assay.received_date": {"value": "2030-04-02", "precision": "DAY"},
                     "assay.report_date": {"value": "2030-04-05", "precision": "DAY"}}
    for field in fields:
        from apps.facts.clinical_schema import validate_value
        validate_value(field.key, field.value)
        for piece in field.fragments:
            assert piece.text == piece.block.text[piece.start:piece.end]
    assert [(row.text, row.polygon) for row in rows] == original


@pytest.mark.parametrize("heading", ["入院记录：上次病理诊断报告书提示结果如下。", "患者自述免疫组化报告。", "报告说明：建议复查免疫组化检测。"])
def test_history_or_explanatory_heading_cannot_create_primary_report(heading):
    rows = report_rows()
    rows[0].text = heading
    segments, unknown, _ = extract(rows)
    assert segments == [] and unknown == {1}


def test_single_block_two_reports_never_borrow_specimen_assay_or_date():
    first = "病理诊断报告书\n标本编号：SYN-A\n检测项目：PD-L1免疫组化\n报告日期：2030-04-05\n检测结果：PD-L1 TPS：13%\n"
    second = "免疫组化检测报告单\n标本编号：SYN-B\n检测项目：PD-L1免疫组化\n检测结果：PD-L1 CPS：21"
    row = block(first + second)
    segments, unknown, fields = extract([row])
    assert len(segments) == 2 and not unknown
    assert min(p.start for p in segments[1].pieces) == len(first)
    assert all(p.end <= len(first) for p in segments[0].pieces)
    assert not any(f.key.endswith("_date") for f in fields[1])
    assert {f.value["raw"] for f in fields[1] if f.key == "specimen.identity"} == {"SYN-B"}


def test_missing_assay_or_multiple_unqualified_specimens_stays_unlinked():
    rows = [block("病理诊断报告书\n标本编号：SYN-A\n标本编号：SYN-B\n检测结果：PD-L1 TPS：13%")]
    _, _, groups = extract(rows)
    score = next(field for field in groups[0] if field.key == "ihc.score")
    assert score.links["SPECIMEN"] is None
    assert score.links["ASSAY"] is None
    assert "unlinked_specimen" in score.limitations


@pytest.mark.parametrize(("raw", "assertion"), [("阴性", "NEGATIVE"), ("未检测", "NOT_TESTED"), ("结果不确定", "UNCERTAIN"), ("阳性", "POSITIVE")])
def test_qualitative_marker_preserves_explicit_assertion_without_numeric_inference(raw, assertion):
    rows = report_rows()[:4] + [block(f"检测结果：PD-L1：{raw}", order=4, box=(.05, .4, .95, .43))]
    _, _, groups = extract(rows)
    result = next(f for f in groups[0] if f.key == "ihc.result")
    assert result.value == {"text": raw, "assertion": assertion}


def test_parallel_report_titles_establish_lanes_and_crossing_text_stays_unassigned():
    rows = [block("病理诊断报告书", order=0, box=(.04, .05, .43, .08)),
            block("免疫组化报告单", order=1, box=(.58, .05, .97, .08)),
            block("标本编号：SYN-A", order=2, box=(.04, .12, .43, .15)),
            block("标本编号：SYN-B", order=3, box=(.58, .12, .97, .15)),
            block("检测结果：PD-L1 TPS：13%", order=4, box=(.04, .2, .43, .23)),
            block("检测结果：PD-L1 CPS：21", order=5, box=(.58, .2, .97, .23)),
            block("报告日期：2030-04-08", order=6, box=(.35, .3, .68, .33))]
    segments, _, groups = extract(rows)
    assert len(segments) == 2
    assert all("parallel_report_unplaced_text" in segment.limitations for segment in segments)
    assert [f.value["score_kind"] for group in groups for f in group if f.key == "ihc.score"] == ["TPS", "CPS"]
    assert not any(f.key.endswith("_date") for group in groups for f in group)


def test_pathology_sections_preserve_negation_and_uncertainty_without_inferred_stage():
    rows = [block("病理诊断报告书\n标本编号：SYN-A\n组织学诊断：可疑腺癌，建议补充染色。\n分化程度：中分化\n切缘：未见肿瘤\n浸润：未见脉管浸润\n病理分期：pT1a（原文记载，待核对）")]
    _, _, groups = extract(rows)
    values = {field.key: field.value for field in groups[0]}
    assert values["specimen.histology"]["assertion"] == "UNCERTAIN"
    assert values["specimen.margin"]["assertion"] == "NEGATIVE"
    assert values["specimen.invasion"]["text"] == "未见脉管浸润"
    assert values["pathology.reported_stage"]["text"] == "pT1a（原文记载，待核对）"
