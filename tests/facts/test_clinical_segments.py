from itertools import count
from types import SimpleNamespace

import pytest

from apps.facts.clinical_segments import segment_reports
from apps.facts.clinical_extraction import field_candidates


_ids = count()


def block(text, *, page=1, order=0, box=(.1, .1, .9, .2)):
    left, top, right, bottom = box
    return SimpleNamespace(pk=next(_ids), text=text, reading_order=order, document_page_id=page, document_page=SimpleNamespace(page_number=page),
                           polygon=[[left, top], [right, top], [right, bottom], [left, bottom]], layout_polygon=None)


@pytest.mark.parametrize("continuation", [
    "CT诊断报告书\n续页 检查号：SYN-42\n诊断意见：左肺结节。",
    "续上页 检查号：SYN-42\n诊断意见：左肺结节。",
])
def test_explicit_continuation_and_matching_exam_id_join_source_pages(continuation):
    rows = [block("CT诊断报告书\n检查号：SYN-42\n影像表现：左肺见结节，约12×8mm。"), block(continuation, page=2)]
    reports, unparsed = segment_reports(rows)
    assert len(reports) == 1 and unparsed == set()
    assert {piece.page for piece in reports[0].pieces} == {1, 2}
    fields = field_candidates(reports[0])
    assert next(f for f in fields if f.key == "imaging.impression").fragments[0].page == 2


@pytest.mark.parametrize("continuation", [
    "诊断意见：没有续页锚点，不能拼到上一页。",
    "续上页 检查号：SYN-43\n诊断意见：不同检查号。",
])
def test_unanchored_or_conflicting_id_page_remains_unparsed(continuation):
    reports, unparsed = segment_reports([block("CT诊断报告书\n检查号：SYN-42\n影像表现：左肺结节。"), block(continuation, page=2)])
    assert len(reports) == 1 and unparsed == {2}
    assert {piece.page for piece in reports[0].pieces} == {1}


def test_parallel_reports_keep_lanes_and_do_not_assign_unplaceable_text():
    rows = [block("CT诊断报告书", order=0, box=(.05, .05, .4, .1)),
            block("MR诊断报告书", order=1, box=(.6, .05, .95, .1)),
            block("影像表现：左肺结节，大小12mm。", order=2, box=(.05, .2, .4, .3)),
            block("影像表现：右额叶结节，大小3mm。", order=3, box=(.6, .2, .95, .3)),
            block("两栏之间无法归属的9mm", order=4, box=(.35, .4, .7, .5))]
    reports, _ = segment_reports(rows)
    assert len(reports) == 2
    assert all("parallel_report_unplaced_text" in report.limitations for report in reports)
    assert [f.value["components"][0]["value"] for report in reports for f in field_candidates(report) if f.key == "lesion.dimensions"] == ["12", "3"]


def test_historical_and_current_sizes_stay_same_entity_with_explicit_time_roles():
    reports, _ = segment_reports([block("CT诊断报告书\n影像表现：左肺见结节，原大小12×9mm，现大小8×6mm。\n诊断意见：左肺结节。")])
    sizes = [f for f in field_candidates(reports[0]) if f.key == "lesion.dimensions"]
    assert [f.value["measurement_role"] for f in sizes] == ["HISTORICAL", "CURRENT"]
    assert len({f.entity for f in sizes}) == 1


def test_normal_organ_size_and_negative_nodule_do_not_become_measured_lesion():
    reports, _ = segment_reports([block("超声检查报告单\n超声所见：左肾未见结节，长径100mm。右肾见囊肿，大小12×9mm；胆囊大小80×30mm，未见结节。\n超声提示：右肾囊肿。")])
    sites = [f.value["text"] for f in field_candidates(reports[0]) if f.key == "lesion.site"]
    sizes = [f.value["components"][0]["value"] for f in field_candidates(reports[0]) if f.key == "lesion.dimensions"]
    assert sites == ["右肾"] and sizes == ["12"]


def test_split_title_and_header_date_follow_geometry_instead_of_provider_column_order():
    rows = [block("CT", order=0, box=(.35, .05, .40, .08)),
            block("诊断", order=1, box=(.405, .05, .46, .08)),
            block("报告书", order=2, box=(.465, .05, .54, .08)),
            block("检查项目：胸部CT平扫", order=3, box=(.05, .18, .4, .21)),
            block("影像表现：左肺结节，约12mm。", order=4, box=(.05, .25, .8, .28)),
            block("检查", order=5, box=(.55, .11, .61, .14)),
            block("日期：2026-08-17", order=6, box=(.612, .11, .85, .14)),
            block("诊断意见：左肺结节。", order=7, box=(.05, .4, .4, .43))]
    reports, unparsed = segment_reports(rows)
    assert len(reports) == 1 and not unparsed
    fields = field_candidates(reports[0])
    assert next(f for f in fields if f.key == "report.exam_date").value["value"] == "2026-08-17"
    assert len([piece for piece in reports[0].pieces if piece.block.reading_order in {0, 1, 2}]) == 3


def test_spaced_title_and_explicit_parenthesized_scope_exclude_method_and_other_metadata():
    reports, _ = segment_reports([block("CT 检查报告单\n检查名称：CT增强（胸部+上腹部）\n检查日期：2026-08-17\n影像表现：左肺结节。\n诊断提示：左肺结节。")])
    assert len(reports) == 1
    field = next(f for f in field_candidates(reports[0]) if f.key == "imaging.body_site")
    assert field.value["text"] == "胸部+上腹部"
    assert "CT增强" in field.raw_value
    assert field.transformations


@pytest.mark.parametrize(("scope", "expected"), [
    ("胸部CT平扫|上腹部CT平扫+动态增强|盆腔CT平扫（常规）", "胸部|上腹部|盆腔"),
    ("上腹部磁共振平扫+动态增强|盆腔磁共振平扫+动态增强+DWI", "上腹部|盆腔"),
    ("彩色多普勒超声检查（常规）腹部（肝胆胰脾肾）", "腹部（肝胆胰脾肾）"),
])
def test_anatomic_scope_retains_all_ordered_regions_and_qualifiers(scope, expected):
    reports, _ = segment_reports([block(f"CT诊断报告书\n检查项目：{scope}\n扫描日期：2026-08-17\n影像表现：左肺结节。\n诊断意见：左肺结节。")])
    field = next(f for f in field_candidates(reports[0]) if f.key == "imaging.body_site")
    assert field.value["text"] == expected
    assert field.transformations and scope in field.raw_value


def test_compound_group_site_and_explicit_lung_segment_are_not_truncated():
    reports, _ = segment_reports([block("CT诊断报告书\n影像表现：纵隔（4R、7组）及双肺门见多发淋巴结，较大者短径约12mm。左肺上叶上舌段见结节，约6mm。\n诊断意见：请核对。")])
    sites = [f.value["text"] for f in field_candidates(reports[0]) if f.key == "lesion.site"]
    assert sites == ["纵隔(4R、7组)及双肺门", "左肺上叶上舌段"]


def adjacent_page_rows():
    return [block("CT诊断报告书", order=0, box=(.35, .18, .65, .21)),
            block("检查日期：2026-08-17", order=1, box=(.62, .22, .95, .25)),
            block("影像所见：", order=2, box=(.05, .28, .22, .31)),
            block("左肺上叶见结节，大小约12mm。", order=25, box=(.0736, .3794, .9755, .4005)),
            block("邻页字", order=26, box=(0, .3815, .0338, .4052)),
            block("诊断意见：左肺结节。", order=27, box=(.05, .55, .65, .58))]


@pytest.mark.parametrize("use_layout_geometry", [False, True])
def test_clipped_edge_column_outside_explicit_body_anchors_stays_unassigned(use_layout_geometry):
    rows = adjacent_page_rows()
    edge = rows[4]
    if use_layout_geometry:
        for row in rows:
            row.layout_polygon = row.polygon
            row.polygon = [[.2, .2], [.8, .2], [.8, .4], [.2, .4]]
    reports, _ = segment_reports(rows)
    assert len(reports) == 1
    report = reports[0]
    fields = [f for f in field_candidates(report) if f.key.startswith("lesion.")]
    assert {f.key for f in fields} == {"lesion.site", "lesion.laterality", "lesion.dimensions"}
    assert all(piece.block.pk != edge.pk for f in fields for piece in f.fragments)
    assert "unassigned_page_edge_text" in report.limitations
    assert all(piece.block.pk != edge.pk for piece in report.pieces)
    assert edge.text == "邻页字"


@pytest.mark.parametrize("case", ["inset_fragment", "wide_left_aligned_text", "one_body_anchor", "edge_report_title"])
def test_page_edge_rule_preserves_text_without_a_separate_unanchored_clipped_column(case):
    rows = adjacent_page_rows()
    edge = rows[4]
    if case == "inset_fragment":
        edge.polygon = [[.015, .3815], [.04, .3815], [.04, .4052], [.015, .4052]]
    elif case == "wide_left_aligned_text":
        edge.polygon = [[0, .3815], [.12, .3815], [.12, .4052], [0, .4052]]
    elif case == "one_body_anchor":
        rows = rows[:-1]
    else:
        edge.text = "超声检查报告单"
    reports, _ = segment_reports(rows)
    assert any(piece.block.pk == edge.pk for report in reports for piece in report.pieces)
    assert all("unassigned_page_edge_text" not in report.limitations for report in reports)
