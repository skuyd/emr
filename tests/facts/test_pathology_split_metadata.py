"""Explicit printed labels, spatially separate values and unknown boundaries."""
from copy import deepcopy

import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract
from tests.facts.test_pathology_named_report_routing import named_report_rows


def split_rows(*, above_title=False):
    title_y, start = (.27, .05) if above_title else (.04, .13)
    items = [
        ("免疫组化检测报告单", (.2, title_y, .8, title_y + .03)),
        ("肿瘤样本编号", (.05, start, .21, start + .03)),
        ("SYN-SPLIT", (.24, start, .43, start + .03)),
        ("样本类型", (.55, start, .69, start + .03)),
        ("合成组织切片", (.73, start, .95, start + .03)),
        ("样本接收日期", (.05, start + .06, .21, start + .09)),
        ("２０３２年０６月０２日", (.24, start + .06, .47, start + .09)),
        ("报告日期", (.55, start + .06, .69, start + .09)),
        ("2032-06-04", (.73, start + .06, .95, start + .09)),
        ("检测项目：PD-L1免疫组化", (.05, .33, .6, .36)),
    ]
    rows = [block(text, order=i, box=box) for i, (text, box) in enumerate(items)]
    rows += named_report_rows()[3:]
    for i, row in enumerate(rows):
        row.reading_order = len(rows) - i  # provider sequence is not label/value ownership
    return rows


@pytest.mark.parametrize('above_title', [False, True])
def test_separate_explicit_labels_keep_independent_unicode_value_and_label(above_title):
    rows = split_rows(above_title=above_title)
    before = deepcopy([(row.text, row.polygon) for row in rows])
    segments, unknown, groups = extract(rows)
    assert len(segments) == 1 and not unknown
    fields = {f.key: f for f in groups[0] if f.key not in {'ihc.score'}}
    expected = {'specimen.identity': ('SYN-SPLIT', '肿瘤样本编号'),
                'specimen.description': ('合成组织切片', '样本类型'),
                'assay.received_date': ('２０３２年０６月０２日', '样本接收日期'),
                'assay.report_date': ('2032-06-04', '报告日期')}
    for key, (value, label) in expected.items():
        candidate = fields[key]
        assert candidate.raw_value == value
        assert '\n'.join(p.text for p in candidate.value_fragments) == value
        assert '\n'.join(p.text for p in candidate.label_fragments) == label
        assert all(p.text == p.block.text[p.start:p.end] for p in candidate.fragments + candidate.label_fragments)
    assert fields['assay.received_date'].value == {'value': '2032-06-02', 'precision': 'DAY'}
    assert fields['assay.report_date'].value == {'value': '2032-06-04', 'precision': 'DAY'}
    assert 'assay.collection_date' not in fields
    assert [(row.text, row.polygon) for row in rows] == before


@pytest.mark.parametrize('problem', ['two_values', 'overlap', 'missing_box', 'next_row', 'other_label'])
def test_uncertain_split_value_does_not_borrow_same_row_or_next_date(problem):
    rows = split_rows()
    target = rows[8]
    if problem == 'two_values':
        rows.append(block('2032-06-05', order=100, box=(.73, .19, .95, .22)))
    elif problem == 'overlap':
        target.polygon = [[.65, .19], [.95, .19], [.95, .22], [.65, .22]]
    elif problem == 'missing_box':
        target.polygon = None
    elif problem == 'next_row':
        target.polygon = [[.73, .24], [.95, .24], [.95, .26], [.73, .26]]
    else:
        target.text = '采样日期'
    _, _, groups = extract(rows)
    assert not [f for group in groups for f in group if f.key == 'assay.report_date']


def test_two_explicit_reports_keep_split_metadata_inside_their_own_lane():
    rows = []
    for panel, identity in enumerate(['SYN-LEFT', 'SYN-RIGHT']):
        for row in split_rows():
            row.text = row.text.replace('SYN-SPLIT', identity)
            row.polygon = [[x * .45 + panel * .55, y] for x, y in row.polygon]
            rows.append(row)
    segments, unknown, groups = extract(rows)
    assert len(segments) == 2 and not unknown
    assert [{f.value['raw'] for f in group if f.key == 'specimen.identity'} for group in groups] == [
        {'SYN-LEFT'}, {'SYN-RIGHT'}]
    for group in groups:
        by_id = {f.node_id: f for f in group}
        identity = next(f for f in group if f.key == 'specimen.identity')
        assert all(by_id[f.links['SPECIMEN']] is identity for f in group if f.key == 'ihc.score')


def test_explicit_inline_metadata_keeps_old_raw_value_with_label():
    from tests.facts.test_pathology_extraction import report_rows
    _, _, groups = extract(report_rows())
    date = next(f for f in groups[0] if f.key == 'assay.report_date')
    assert date.raw_value == '报告日期：2030-04-05'
    assert '\n'.join(p.text for p in date.value_fragments) == '2030-04-05'
    assert '\n'.join(p.text for p in date.label_fragments) == '报告日期：'
