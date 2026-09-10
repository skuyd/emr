from decimal import Decimal

import pytest

from apps.cancer_ordering.matching import eligible_for_auto, literal_candidates


@pytest.mark.parametrize(('suffix', 'assertion'), [
    ('均未见', 'NEGATED'), ('均未证实', 'NEGATED'), ('均已排除', 'NEGATED'), ('均为疑似', 'UNCERTAIN'),
    ('皆未检出', 'NEGATED'), ('均难以判定', 'UNKNOWN'),
])
def test_explicit_shared_suffix_qualifies_both_named_diagnoses(suffix, assertion):
    raw = '临床诊断：肺癌及胰腺癌' + suffix + '。'
    rows = literal_candidates(raw, 'DIAGNOSIS')
    assert [(row['profile'], row['assertion']) for row in rows] == [('LUNG', assertion), ('PANCREAS', assertion)]
    assert all(not eligible_for_auto(row, (Decimal('.99'),), source_valid=True) for row in rows)
    assert all(raw[row['start']:row['end']] == row['raw'] for row in rows)


@pytest.mark.parametrize('person', ['丈夫', '妻子', '儿子', '女儿', '兄长'])
def test_reported_other_person_is_explicitly_separate_from_the_patient(person):
    rows = literal_candidates('临床诊断：' + person + '患肺癌。', 'DIAGNOSIS')
    assert len(rows) == 1 and rows[0]['subject'] == 'OTHER_PERSON'
    assert not eligible_for_auto(rows[0], (Decimal('.99'),), source_valid=True, reviewed=True)


@pytest.mark.parametrize('prefix', ['某关系人患', '同行者患', '拒绝诊断为'])
def test_unsupported_subject_or_predicate_prefix_does_not_get_default_patient_identity(prefix):
    rows = literal_candidates('临床诊断：' + prefix + '肺癌。', 'DIAGNOSIS')
    assert len(rows) == 1 and rows[0]['subject'] == 'UNKNOWN'
    assert not eligible_for_auto(rows[0], (Decimal('.99'),), source_valid=True, reviewed=True)


def test_shared_group_does_not_cross_explicit_contrast_or_a_different_finding():
    rows = literal_candidates('临床诊断：肺癌明确，但胰腺癌及胰头癌均未见。', 'DIAGNOSIS')
    assert [row['assertion'] for row in rows] == ['AFFIRMED', 'NEGATED', 'NEGATED']
    assert eligible_for_auto(rows[0], (Decimal('.99'),), source_valid=True)
    current = literal_candidates('临床诊断：患者患肺癌。', 'DIAGNOSIS')
    assert len(current) == 1 and eligible_for_auto(current[0], (Decimal('.99'),), source_valid=True)


@pytest.mark.parametrize('raw', ['初步诊断：肺癌及胰腺癌均明确。', '临床诊断：肺癌或胰腺癌均明确。'])
def test_shared_affirmative_tail_does_not_erase_preliminary_or_disjunctive_scope(raw):
    rows = literal_candidates(raw, 'DIAGNOSIS')
    assert len(rows) == 2 and all(row['assertion'] == 'UNCERTAIN' for row in rows)
