"""Finite headed narratives retain negation, object and time evidence."""
import pytest

from apps.cancer_ordering.matching import ALIASES, eligible_for_auto, literal_candidates
from .test_narrative_sources import assert_original_fragments, block, discover


def candidates(text):
    from apps.cancer_ordering.narrative_matching import narrative_candidates
    sources = discover([block(text)]).inputs
    return [(source, row) for source in sources for row in narrative_candidates(source)]


@pytest.mark.parametrize('text,label,assertion,subject', [
    ('主诉：肺癌治疗后不适。', '肺癌', 'AFFIRMED', 'CURRENT_PRIMARY'),
    ('主诉：疑似肺癌。', '肺癌', 'UNCERTAIN', 'CURRENT_PRIMARY'),
    ('主诉：否认肺癌。', '肺癌', 'NEGATED', 'CURRENT_PRIMARY'),
    ('主诉：既往肺癌术后。', '肺癌', 'AFFIRMED', 'HISTORICAL'),
    ('主诉：父亲患肺癌。', '肺癌', 'AFFIRMED', 'OTHER_PERSON'),
    ('主诉：同事患肺癌。', '肺癌', 'AFFIRMED', 'UNKNOWN'),
    ('现病史：患者诊断为胰头癌。', '胰头癌', 'AFFIRMED', 'CURRENT_PRIMARY'),
    ('现病史：患者既往诊断为胰头癌。', '胰头癌', 'AFFIRMED', 'HISTORICAL'),
    ('现病史：胰腺癌。', '胰腺癌', 'AFFIRMED', 'UNKNOWN'),
    ('会诊意见：病史摘要：患者诊断为肺癌。', '肺癌', 'AFFIRMED', 'CURRENT_PRIMARY'),
    ('会诊意见：病史摘要：患者既往曾诊断为肺癌。', '肺癌', 'AFFIRMED', 'HISTORICAL'),
    ('首次病程记录：患者因肺癌收入院。', '肺癌', 'AFFIRMED', 'CURRENT_PRIMARY'),
    ('首次病程记录：患者因疑似肺癌入院。', '肺癌', 'UNCERTAIN', 'CURRENT_PRIMARY'),
    ('辅助检查：CT检查结果考虑肺癌。', '肺癌', 'UNCERTAIN', 'UNKNOWN'),
    ('主诉：转移性肺癌。', '肺癌', 'AFFIRMED', 'METASTATIC_SITE'),
])
def test_only_reported_literal_and_actual_context_determine_assertion_and_subject(text, label, assertion, subject):
    source_row, = candidates(text)
    source, row = source_row
    assert (row['label'], row['assertion'], row['subject']) == (label, assertion, subject)
    assert row['profile'] == ALIASES[label]
    assert row['raw'] == source.text[row['start']:row['end']]
    assert row['label_raw'] == source.text[row['match_start']:row['match_end']]


@pytest.mark.parametrize('text', [
    '主诉：化疗后不适。', '辅助检查：CEA 20，CA19-9 60。',
    '临床用药：肺癌适用合成药物。', '治疗适应证：肺癌。',
    '送检者提供诊断：肺癌。', '参考文献：肺癌。', '图谱：肺癌。',
    '仅为合成页：患者诊断为肺癌。',
])
def test_medication_labs_reference_and_unheaded_text_do_not_supply_diagnoses(text):
    assert candidates(text) == []


@pytest.mark.parametrize('ending,assertion', [('均未见', 'NEGATED'), ('均考虑', 'UNCERTAIN')])
def test_shared_tail_applies_to_the_whole_coordinated_narrative(ending, assertion):
    rows = [row for _, row in candidates(f'主诉：肺癌及胰腺癌{ending}。')]
    assert [row['label'] for row in rows] == ['肺癌', '胰腺癌']
    assert all(row['assertion'] == assertion for row in rows)


def test_new_explicit_assertion_does_not_inherit_previous_negation():
    rows = [row for _, row in candidates('主诉：未见肺癌，但患者诊断为胰腺癌。')]
    assert [(row['label'], row['assertion']) for row in rows] == [('肺癌', 'NEGATED'), ('胰腺癌', 'AFFIRMED')]


def test_unsupported_longer_diagnosis_does_not_borrow_an_inner_alias():
    _, row = candidates('主诉：非小细胞肺癌。')[0]
    assert row['profile'] is None and row['label'] == '非小细胞肺癌'


def test_nfkc_view_does_not_shift_the_original_label_after_expanding_characters():
    original = block('主诉：Ⅳ期； 患者诊断为肺癌。')
    from apps.cancer_ordering.narrative_matching import narrative_candidates
    source, = discover([original]).inputs
    row, = narrative_candidates(source)
    binding = source.candidate_binding(row)
    assert binding['label_fragments'][0]['start'] == original.text.index('肺癌')
    assert_original_fragments(binding['label_fragments'], [original])


@pytest.mark.parametrize('record_date,subject', [('2025-02-01', 'HISTORICAL'),
    ('2024-01-01', 'CURRENT_PRIMARY'), (None, 'UNKNOWN')])
def test_event_date_needs_a_located_same_record_date_before_it_proves_time(record_date, subject):
    lines = ['首次病程记录']
    if record_date:
        lines.append('记录日期：' + record_date)
    lines.extend(['现病史：2024-01-01患者诊断为肺癌。'])
    _, row = candidates('\n'.join(lines))[0]
    assert row['subject'] == subject


@pytest.mark.parametrize('confidence', ['0.9499', None])
def test_low_confidence_governing_heading_cannot_borrow_the_label_confidence(confidence):
    from apps.cancer_ordering.narrative_matching import narrative_candidates
    blocks = [block('主诉：', 0, confidence=confidence), block('肺癌治疗后不适。', 1)]
    source, = discover(blocks).inputs
    row, = narrative_candidates(source)
    binding = source.candidate_binding(row)
    assert not eligible_for_auto(row, binding['confidence_values'], source_valid=True)
    assert_original_fragments(binding['heading_fragments'], blocks)


def test_old_matching_contract_and_five_literal_catalog_stay_separate():
    assert len(ALIASES) == 5
    assert literal_candidates('主诉：肺癌。', 'DIAGNOSIS') == ()
    assert literal_candidates('出院诊断：肺癌。', 'DIAGNOSIS')[0]['subject'] == 'CURRENT_PRIMARY'
    assert candidates('主诉：肺癌。')[0][1]['rule_version'].startswith('reported-cancer-narratives-')


@pytest.mark.parametrize('heading', ['现病史', '会诊意见：病史摘要'])
def test_recognized_patient_verb_does_not_erase_a_later_unknown_subject(heading):
    _, row = candidates(heading + '：患者诊断为同事患肺癌。')[0]
    assert row['subject'] == 'UNKNOWN'


def test_auxiliary_date_and_result_verb_do_not_erase_an_unknown_exam_subject():
    _, row = candidates('首次病程记录\n记录日期：2024-01-01\n辅助检查：2024-01-01邻居CT结果提示肺癌。')[0]
    assert row['subject'] == 'UNKNOWN'


def test_symptom_word_does_not_erase_a_real_negative_disease_assertion():
    _, row = candidates('主诉：未见肺癌，治疗后不适。')[0]
    assert row['assertion'] == 'NEGATED'


def test_slash_date_is_not_a_disjunctive_diagnosis_operator():
    _, row = candidates('首次病程记录\n记录日期：2024/1/1\n现病史：2024/1/1患者诊断为肺癌。')[0]
    assert row['assertion'] == 'AFFIRMED' and row['subject'] == 'CURRENT_PRIMARY'


def test_conflicting_record_dates_cannot_supply_temporal_qualification():
    _, row = candidates('首次病程记录\n记录日期：2024-01-01\n记录时间：2025-01-01\n现病史：2024-01-01患者诊断为肺癌。')[0]
    assert row['subject'] == 'UNKNOWN'
