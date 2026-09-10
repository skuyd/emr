"""Headed slots must not lend their role to unrelated original text."""
from copy import deepcopy

import pytest

from apps.cancer_ordering.matching import eligible_for_auto
from apps.cancer_ordering.narrative_matching import narrative_candidates
from .test_narrative_sources import assert_original_fragments, block, discover


def rows(result):
    return [(source, row) for source in result.inputs for row in narrative_candidates(source)]


@pytest.mark.parametrize('boundary,separator', [
    ('未知栏目：合成说明。', '\n'),
    ('日常病程记录：合成记录。', '\n'),
    ('中医辨证：合成说明。', '\n'),
    ('未知栏目：合成说明。', ' '),
    ('未知栏目：合成说明。', ''),
    ('【未识别栏目】：合成说明。', '\n'),
    ('3.其他栏目：合成说明。', '\n'),
])
def test_unsupported_heading_with_body_closes_prior_slot(boundary, separator):
    original = block('主诉：肺癌。' + separator + boundary + '\n胰腺癌。')
    result = discover([original])
    assert [row['label'] for _, row in rows(result)] == ['肺癌']
    assert all('合成' not in source.body and '胰腺癌' not in source.text for source in result.inputs)
    assert result.coverage[0]['status'] == 'UNJUDGED' and not result.complete


def test_supported_new_heading_can_start_after_an_unjudged_section():
    result = discover([block('主诉：肺癌。\n未知栏目：胰腺癌。\n现病史：患者诊断为胰头癌。')])
    assert [(source.role, row['label']) for source, row in rows(result)] == [
        ('CHIEF_COMPLAINT', '肺癌'), ('PRESENT_ILLNESS', '胰头癌')]
    assert result.coverage[0]['status'] == 'UNJUDGED'


@pytest.mark.parametrize('punctuation', ['。', '；', '！', '?'])
@pytest.mark.parametrize('independent_first', [False, True])
def test_admission_slot_excludes_independent_sentences_on_either_side(punctuation, independent_first):
    introduction = '患者因头痛入院' + punctuation
    independent = '患者诊断为肺癌' + punctuation
    text = independent + introduction if independent_first else introduction + independent
    result = discover([block('首次病程记录：' + text)])
    assert rows(result) == []
    assert len(result.inputs) == 1 and result.inputs[0].body == introduction
    assert '肺癌' not in result.inputs[0].text
    assert result.coverage[0]['status'] == 'UNJUDGED'


def test_admission_disease_is_only_from_its_own_sentence_with_original_offsets():
    original = block('首次病程记录：患者诊断为肺癌。患者因胰腺癌入院。患者诊断为肺癌。')
    before = deepcopy(vars(original))
    result = discover([original])
    source, row = rows(result)[0]
    assert len(rows(result)) == 1 and row['label'] == '胰腺癌'
    assert source.body == '患者因胰腺癌入院。'
    binding = source.candidate_binding(row)
    assert binding['label_fragments'][0]['start'] == original.text.index('胰腺癌')
    assert_original_fragments(binding['section_fragments'], [original])
    assert vars(original) == before


@pytest.mark.parametrize('prefix,assertion,subject', [
    ('患者因', 'AFFIRMED', 'CURRENT_PRIMARY'),
    ('患者因疑似', 'UNCERTAIN', 'CURRENT_PRIMARY'),
    ('患者因否认', 'NEGATED', 'CURRENT_PRIMARY'),
    ('患者此前因', 'AFFIRMED', 'HISTORICAL'),
    ('父亲因', 'AFFIRMED', 'OTHER_PERSON'),
    ('同事因', 'AFFIRMED', 'UNKNOWN'),
])
def test_wrapped_intro_retains_its_actual_subject_and_assertion(prefix, assertion, subject):
    original = block('首次病程记录：' + prefix + '肺癌\r\n收入院。患者诊断为胰腺癌。')
    result = discover([original])
    source, row = rows(result)[0]
    assert len(rows(result)) == 1 and row['label'] == '肺癌'
    assert (row['assertion'], row['subject']) == (assertion, subject)
    assert '\r\n' in source.body and '胰腺癌' not in source.text
    binding = source.candidate_binding(row)
    assert_original_fragments(binding['section_fragments'], [original])
    if assertion != 'AFFIRMED' or subject != 'CURRENT_PRIMARY':
        assert not eligible_for_auto(row, binding['confidence_values'], source_valid=True)


def test_wrapped_intro_across_adjacent_blocks_keeps_real_context_geometry_and_confidence():
    blocks = [block('首次病程记录：患者因疑似', 0, confidence='.9499'),
              block('肺癌收入院。患者诊断为胰腺癌。', 1)]
    result = discover(blocks)
    source, row = rows(result)[0]
    assert len(rows(result)) == 1 and row['label'] == '肺癌' and row['assertion'] == 'UNCERTAIN'
    binding = source.candidate_binding(row)
    assert_original_fragments(binding['section_fragments'], blocks)
    assert {fragment['block_id'] for fragment in binding['section_fragments']} == {str(item.pk) for item in blocks}
    assert not eligible_for_auto(row, binding['confidence_values'], source_valid=True)
    assert '胰腺癌' not in source.text


def test_repeated_intro_sentences_keep_distinct_original_occurrences():
    original = block('首次病程记录：患者因肺癌入院。合成记录。患者因肺癌入院。')
    result = discover([original])
    assert len(result.inputs) == 2
    offsets = [source.candidate_binding(row)['label_fragments'][0]['start'] for source, row in rows(result)]
    assert offsets == [original.text.index('肺癌'), original.text.rindex('肺癌')]
    assert all(source.body == '患者因肺癌入院。' for source in result.inputs)


def test_unproved_admission_body_has_no_invented_slot_but_keeps_unknown_inventory():
    result = discover([block('首次病程记录：患者诊断为肺癌。')])
    assert not result.inputs and not result.complete
    assert result.coverage[0]['status'] == 'UNJUDGED'
