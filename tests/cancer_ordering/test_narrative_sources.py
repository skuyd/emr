"""The narrative adapter carries actual character positions across source lines."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import pytest


def block(text, order=0, *, page='page-a', version='version-a', x=.08, y=None,
          width=.72, confidence='0.99'):
    top = .10 + order * .045 if y is None else y
    polygon = [[x, top], [x + width, top], [x + width, top + .025], [x, top + .025]]
    return SimpleNamespace(pk=f'{page}-block-{order}', document_page_id=page,
        parsing_version_id=version, reading_order=order, text=text, polygon=polygon,
        layout_polygon=deepcopy(polygon), confidence=Decimal(confidence) if confidence is not None else None)


def discover(blocks):
    from apps.cancer_ordering.narrative_layout import discover_narratives
    return discover_narratives(blocks)


def assert_original_fragments(fragments, blocks):
    by_id = {str(item.pk): item for item in blocks}
    assert fragments
    for fragment in fragments:
        original = by_id[fragment['block_id']]
        assert fragment['page_id'] == str(original.document_page_id)
        assert fragment['version_id'] == str(original.parsing_version_id)
        assert fragment['raw'] == original.text[fragment['start']:fragment['end']]
        assert fragment['polygon'] == original.polygon


def test_split_heading_and_value_are_bound_to_their_real_source_characters():
    blocks = [block('主诉：', 0, width=.09), block('肺癌治疗后不适', 1, x=.18, y=.10, width=.25)]
    before = deepcopy([vars(item) for item in blocks])
    result = discover(blocks)
    assert result.complete and len(result.inputs) == 1
    source = result.inputs[0]
    assert source.role == 'CHIEF_COMPLAINT' and source.body.strip() == '肺癌治疗后不适'
    assert_original_fragments(source.heading_fragments, blocks)
    assert_original_fragments(source.fragments(source.body_start, len(source.text)), blocks)
    assert [vars(item) for item in blocks] == before


def test_multiline_block_preserves_repeated_sentence_offsets():
    from apps.cancer_ordering.narrative_matching import narrative_candidates
    original = block('主诉：肺癌。\r\n肺癌。')
    source, = discover([original]).inputs
    rows = narrative_candidates(source)
    assert len(rows) == 2
    bindings = [source.candidate_binding(row) for row in rows]
    offsets = [row['label_fragments'][0]['start'] for row in bindings]
    assert offsets == [3, 8]
    for binding in bindings:
        assert_original_fragments(binding['label_fragments'], [original])
        assert_original_fragments(binding['fragments'], [original])


def test_same_page_records_do_not_share_their_parent_heading_or_dates():
    blocks = [block('会诊记录', 0), block('记录日期：2025-02-01', 1),
              block('病史摘要：患者诊断为肺癌。', 2), block('首次病程记录', 3),
              block('患者因胰腺癌收入院。', 4)]
    first, second = discover(blocks).inputs
    assert first.role == 'CONSULTATION_SUMMARY' and second.role == 'ADMISSION_NARRATIVE'
    assert first.record_dates and not second.record_dates
    assert '会诊' not in second.text and '肺癌' not in second.text


def test_nested_history_requires_the_real_parent_record_heading():
    result = discover([block('病例特点', 0), block('1.病史：患者诊断为肺癌。', 1)])
    source, = result.inputs
    assert source.role == 'PRESENT_ILLNESS' and '病例特点' in source.text
    assert not discover([block('病史：患者诊断为肺癌。')]).inputs


def test_same_line_nested_consultation_heading_retains_all_original_proof():
    original = block('会诊意见：病史摘要：患者诊断为肺癌。')
    source, = discover([original]).inputs
    assert source.body == '患者诊断为肺癌。'
    assert source.text == original.text
    assert ''.join(item['raw'] for item in source.heading_fragments).replace('：', '') == '会诊意见病史摘要'


def test_inline_next_section_stops_the_original_narrative():
    source, = discover([block('主诉：肺癌。 查体：胰腺癌。')]).inputs
    assert source.body.strip() == '肺癌。' and '胰腺癌' not in source.text


def test_neighbouring_column_cannot_supply_a_heading_or_continuation():
    result = discover([block('主诉：', 0, x=.05, width=.08),
                       block('肺癌。', 1, x=.70, y=.10, width=.15)])
    assert not any('肺癌' in source.body for source in result.inputs)
    assert any(row['status'] == 'UNJUDGED' for row in result.coverage)


def test_cross_page_continuation_keeps_unknown_coverage_without_guessing():
    result = discover([block('主诉：', page='page-a'), block('肺癌。', page='page-b')])
    assert not result.inputs
    assert len(result.coverage) == 2
    assert all(row['status'] == 'UNJUDGED' for row in result.coverage)


def test_large_vertical_gap_cannot_extend_the_previous_source():
    result = discover([block('主诉：', 0), block('肺癌。', 1, y=.70)])
    assert not result.inputs and not result.complete


def test_zero_literal_narrative_is_in_inventory_and_unheaded_page_is_unknown():
    result = discover([block('主诉：头痛。')])
    assert result.complete and len(result.inputs) == 1
    assert result.coverage[0]['status'] == 'SCOPED'
    unknown = discover([block('患者诊断为肺癌。')])
    assert not unknown.inputs and unknown.coverage[0]['status'] == 'UNJUDGED'


def test_excluded_sections_and_new_real_section_are_separate():
    result = discover([block('质控说明：', 0), block('主诉：肺癌。', 1),
                       block('首次病程记录', 2), block('主诉：胰腺癌。', 3)])
    assert len(result.inputs) == 1 and result.inputs[0].body == '胰腺癌。'


def test_invalid_geometry_is_explicitly_incomplete_without_page_claims():
    original = block('主诉：肺癌。')
    original.polygon = original.layout_polygon = []
    result = discover([original])
    assert not result.inputs and not result.complete
    assert 'invalid_geometry' in result.coverage[0]['reasons']


def test_each_page_input_is_bound_to_one_actual_parsing_version():
    with pytest.raises(ValueError, match='解析'):
        discover([block('主诉：', 0), block('肺癌。', 1, version='version-b')])


@pytest.mark.parametrize('title,body', [('病例特点', '病史：患者诊断为肺癌。'),
                                      ('会诊意见', '病史摘要：患者诊断为肺癌。')])
def test_child_heading_cannot_reopen_excluded_material_as_a_new_record(title, body):
    result = discover([block('质控说明', 0), block(title, 1), block(body, 2)])
    assert not result.inputs


@pytest.mark.parametrize('boundary', ['未知栏目：', '日常病程记录'])
def test_unknown_section_and_new_unhandled_document_title_end_a_source(boundary):
    from apps.cancer_ordering.narrative_matching import narrative_candidates
    result = discover([block('主诉：肺癌。\n' + boundary + '\n胰腺癌。')])
    assert [row['label'] for source in result.inputs for row in narrative_candidates(source)] == ['肺癌']
    assert result.coverage[0]['status'] == 'UNJUDGED'


@pytest.mark.parametrize('change', ['text', 'page', 'version', 'unmapped_character'])
def test_source_value_cannot_claim_characters_or_identity_not_in_its_mapping(change):
    from dataclasses import replace
    source, = discover([block('主诉：肺癌。')]).inputs
    values = {'text': {'text': source.text.replace('肺癌', '胃癌')},
              'page': {'page_id': 'other-page'}, 'version': {'version_id': 'other-version'},
              'unmapped_character': {'positions': (None, *source.positions[1:])}}
    with pytest.raises(ValueError, match='原文|原页|解析'):
        replace(source, **values[change])
