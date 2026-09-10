from copy import deepcopy

import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract


def table_rows():
    items = [
        ("免疫组化检测报告单", (.2, .04, .8, .08)),
        ("标本编号：", (.05, .12, .2, .15)), ("SYN-A", (.21, .12, .36, .15)),
        ("检测项目：", (.05, .18, .2, .21)), ("PD-L1免疫组化", (.21, .18, .6, .21)),
        ("抗体名称", (.05, .3, .2, .33)), ("克隆号", (.26, .3, .42, .33)),
        ("检测方法", (.49, .3, .64, .33)), ("检测结果", (.73, .3, .96, .33)),
        ("TPS：13%", (.73, .39, .96, .42)),
        ("PD-L1", (.05, .43, .2, .46)), ("SYN-CLONE", (.26, .43, .42, .46)),
        ("IHC", (.49, .43, .64, .46)),
        ("CPS：21", (.73, .47, .96, .5)),
        ("质控结果", (.05, .56, .22, .59)),
        ("PD-L1", (.05, .65, .2, .68)), ("TPS：100%", (.73, .65, .96, .68)),
    ]
    return [block(text, order=i, box=box) for i, (text, box) in enumerate(items)]


def test_table_headers_bind_two_score_cells_without_borrowing_quality_control():
    rows = table_rows()
    originals = deepcopy([(row.text, row.polygon) for row in rows])
    segments, _, groups = extract(rows)
    assert len(segments) == 1
    scores = [item for item in groups[0] if item.key == "ihc.score"]
    assert [(item.value["score_kind"], item.value["values"]) for item in scores] == [("TPS", ["13"]), ("CPS", ["21"])]
    assert len({item.entity for item in scores}) == 1
    assert all(item.links["SPECIMEN"] and item.links["ASSAY"] and item.links["MARKER"] for item in scores)
    assert [(row.text, row.polygon) for row in rows] == originals


def test_table_layout_coordinates_drive_association_but_original_polygons_remain_sources():
    rows = table_rows()
    for row in rows:
        row.layout_polygon = row.polygon
        row.polygon = [[.15, .2], [.8, .2], [.8, .4], [.15, .4]]
    _, _, groups = extract(rows)
    scores = [item for item in groups[0] if item.key == "ihc.score"]
    assert len(scores) == 2
    assert all(piece.block.polygon == [[.15, .2], [.8, .2], [.8, .4], [.15, .4]] for score in scores for piece in score.fragments)


def test_overlapping_table_cell_and_unaligned_second_marker_do_not_choose_nearest():
    rows = table_rows()
    rows.insert(14, block("Ki-67", order=40, box=(.05, .51, .2, .54)))
    rows[9].polygon = [[.55, .39], [.96, .39], [.96, .42], [.55, .42]]
    _, _, groups = extract(rows)
    # First score crosses explicit method/result columns. Second has no
    # aligned marker row once the table has two distinct marker anchors.
    assert not [item for item in groups[0] if item.key == "ihc.score"]


@pytest.mark.parametrize("result", ["PD-L1-AS1 TPS：13%", "PD-L10 TPS：13%", "非PD-L1 TPS：13%", "既往PD-L1 TPS：13%"])
def test_result_like_suffixes_or_historical_context_do_not_shorten_marker_name(result):
    rows = table_rows()[:5] + [block("检测结果：" + result, order=10, box=(.05, .4, .96, .43))]
    _, _, groups = extract(rows)
    assert not [item for item in groups[0] if item.key == "ihc.score"]


@pytest.mark.parametrize("tail", ["未检测TPS：0", "TPS：待测，参考13%", "阳性对照TPS：100%", "TPS：≤1-5%"])
def test_not_tested_control_and_ambiguous_numeric_grammar_are_not_current_scores(tail):
    rows = table_rows()[:5] + [block("检测结果：PD-L1 " + tail, order=10, box=(.05, .4, .96, .43))]
    _, _, groups = extract(rows)
    assert not [item for item in groups[0] if item.key == "ihc.score"]
