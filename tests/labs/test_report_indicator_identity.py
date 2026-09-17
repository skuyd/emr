import pytest

from apps.exports.content import build_snapshot
from apps.labs.comparison import comparison_view
from apps.labs.dictionary import current_dictionary
from apps.labs.reports import decide_relation, report_relations
from apps.labs.revisions import effective_observation, revise_observation
from apps.labs.validation import validate_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def albumin_report(patient, *, raw_name='白蛋白'):
    document, row, unit = report(patient, value='35')
    dictionary = current_dictionary()
    definition = dictionary.match('白蛋白', specimen='BLOOD')
    row.raw_name = raw_name
    row.standard_code = definition.code
    row.standard_name = definition.standard_name
    row.dictionary_version = dictionary.version
    row.raw_unit = 'g/L'
    row.reference_range_raw = '30-50'
    row.save()
    row.evidence.source_text = f'{raw_name} 35 g/L 30-50'
    row.evidence.save(update_fields=['source_text'])
    return document, row, unit


def test_known_name_mapping_disagreement_blocks_merging_and_folding(django_user_model):
    client, patient = _patient(django_user_model, 'indicator-name-disagreement')
    _, first, _ = albumin_report(patient)
    _, suspect, _ = albumin_report(patient, raw_name='前白蛋白')
    issues = validate_observation(effective_observation(suspect))
    assert any(item['code'] == 'association_conflict' and item['rule_id'] == 'indicator_identity'
               for item in issues)
    relation, = report_relations(patient)
    assert relation.state == 'REVIEW'
    table = comparison_view(patient)
    assert table.result_count == 2
    assert table.report_count == 2
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert len(snapshot['lab_results']) == 2
    assert {key for item in snapshot['lab_results'] for key in item['source_ids']} == {str(first.pk), str(suspect.pk)}
    response = client.get(f'/labs/observations/{suspect.pk}/')
    assert response.status_code == 200
    content = response.content.decode()
    for text in ('前白蛋白', '白蛋白', '35', 'g/L', '指标身份待核对'):
        assert text in content
    suspect.refresh_from_db()
    assert suspect.standard_code == first.standard_code
    assert suspect.raw_name == '前白蛋白'


@pytest.mark.parametrize('changes', [{'raw_name': '白蛋白'}, {'standard_code': 'LAB_ALB'}])
def test_audited_indicator_correction_recomputes_relation_and_folding(django_user_model, changes):
    _, patient = _patient(django_user_model, 'indicator-name-correction')
    albumin_report(patient)
    _, suspect, _ = albumin_report(patient, raw_name='前白蛋白')
    assert report_relations(patient)[0].state == 'REVIEW'
    revise_observation(patient.account, suspect.pk, action='CORRECT', changes=changes, expected_revision=0)
    suspect.refresh_from_db()
    effective = effective_observation(suspect)
    assert not any(item['rule_id'] == 'indicator_identity' for item in validate_observation(effective))
    assert comparison_view(patient).result_count == 1
    relation, = report_relations(patient)
    assert not relation.basis['identity_or_result_uncertain']
    assert relation.state == 'REVIEW'
    decide_relation(patient, patient.account, relation.pk, 'SAME', expected_revision=relation.revision_number,
                    rationale='对照原件更正指标后确认同一报告', operation_id='confirm-corrected-indicator')
    assert comparison_view(patient).report_count == 1
    assert suspect.raw_name == '前白蛋白'
    assert suspect.revisions.count() == 1
