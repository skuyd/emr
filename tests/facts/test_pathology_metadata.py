import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract
from tests.facts.test_pathology_tables import table_rows


def test_named_ihc_title_and_explicit_single_material_do_not_require_invented_identifiers():
    rows = [block("样本类型：组织切片", order=0, box=(.05, .05, .5, .08)),
            block("PD-L1免疫组化检测", order=1, box=(.2, .12, .8, .15)),
            block("检测结果：PD-L1 TPS：13% CPS：21", order=2, box=(.05, .3, .95, .33)),
            block("报告编号：SYN-RPT", order=3, box=(.05, .5, .5, .53))]
    segments, unknown, groups = extract(rows)
    assert len(segments) == 1 and not unknown
    fields = groups[0]
    assert next(field for field in fields if field.key == "specimen.identity").value["raw"] == "组织切片"
    assert next(field for field in fields if field.key == "assay.identity").value["raw"] == "PD-L1免疫组化检测"
    assert all(field.links["SPECIMEN"] and field.links["ASSAY"] for field in fields if field.key == "ihc.score")
    assert segments[0].pieces[0].block == rows[0]


@pytest.mark.parametrize("title", ["病史记载：PD-L1免疫组化检测", "送检建议：PD-L1免疫组化检测", "PD-L1免疫组化检测的判读说明"])
def test_named_assay_phrase_inside_history_or_instructions_is_not_primary_title(title):
    rows = [block(title + "\n检测结果：PD-L1 TPS：13%")]
    segments, unknown, _ = extract(rows)
    assert not segments and unknown == {1}


def test_explicit_collection_label_with_missing_value_never_borrows_next_date_row():
    rows = table_rows()[:5] + [block("采样日期：", order=8, box=(.05, .23, .2, .25)),
                            block("收样日期：2030-04-02", order=9, box=(.05, .27, .4, .29)),
                            block("检测结果：PD-L1 TPS：13%", order=10, box=(.05, .4, .9, .43))]
    _, _, groups = extract(rows)
    dates = {field.key: field.value for field in groups[0] if field.key.endswith("_date")}
    assert dates == {"assay.received_date": {"value": "2030-04-02", "precision": "DAY"}}


def test_similar_specimen_identifier_does_not_match_another_identifier_prefix():
    rows = [block("病理诊断报告书\n标本编号：SYN-A\n标本编号：SYN-AA\n检测项目：SYN-AA PD-L1免疫组化\n检测结果：SYN-AA PD-L1 TPS：13%")]
    _, _, groups = extract(rows)
    by_id = {field.node_id: field for field in groups[0]}
    score = next(field for field in groups[0] if field.key == "ihc.score")
    assert by_id[score.links["SPECIMEN"]].value["raw"] == "SYN-AA"


@pytest.mark.parametrize("raw", ["13abc", "13.4.5%", "13%-20%", "13mg", "13+", "13/100"])
def test_score_parser_does_not_truncate_unsupported_numeric_or_unit_suffix(raw):
    rows = table_rows()[:5] + [block("检测结果：PD-L1 TPS：" + raw, order=10, box=(.05, .4, .9, .43))]
    _, _, groups = extract(rows)
    assert not [field for field in groups[0] if field.key == "ihc.score"]
