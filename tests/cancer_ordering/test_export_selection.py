from collections import Counter

from django.core.exceptions import PermissionDenied
import pytest

from apps.cancer_ordering.readmodels import resolve_ordering
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, ExportUnavailable, SnapshotChanged
from apps.exports.formats import json_bytes
from apps.exports.services import create_preview, get_preview
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from tests.cancer_ordering.test_lab_ordering import labs
from tests.cancer_ordering.test_services import _collect, _row, _revise, _select
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def selected_body(patient, **changes):
    return {'mode': 'documents', 'document_ids': [], 'sections': ['cancer_ordering'],
            'cancer_expected_fingerprint': resolve_ordering(patient)['fingerprint'], **changes}


@pytest.mark.parametrize('profile,first', [('LUNG', 'LAB_CEA'), ('PANCREAS', 'LAB_CA19_9')])
def test_actual_snapshot_prioritizes_only_selected_rows_without_changing_values_or_card_ids(django_user_model, profile, first):
    _, patient = _patient(django_user_model, 'cancer-export-sort-' + profile)
    rows = labs(patient)
    _collect(patient)
    chosen = [row for row in rows if row.standard_code in {'LAB_WBC', 'LAB_CEA', 'LAB_CA19_9'}]
    selection = {'mode': 'documents', 'document_ids': [str(row.parsing_version.document_id) for row in rows],
                 'observation_ids': [str(row.pk) for row in chosen], 'sections': ['labs'], 'details': True}
    _select(patient, 'GENERAL')
    original = build_snapshot(patient, selection)
    _select(patient, 'MANUAL_PROFILE', profile=profile)
    actual = build_snapshot(patient, selection)
    assert actual['labs'][0]['standard_code'] == first
    assert {row['id']: row for row in actual['labs']} == {row['id']: row for row in original['labs']}
    assert Counter(actual['card']['lab_ids']) == Counter(original['card']['lab_ids'])
    lookup = {row['id']: row for row in actual['labs']}
    assert lookup[actual['card']['lab_ids'][0]]['standard_code'] == first
    assert not actual['cancer_candidates'] and not actual['indicator_ordering']
    assert actual['cancer_ordering_fingerprint'] == resolve_ordering(patient)['fingerprint']
    portable = json_bytes(actual).decode()
    assert '肺癌' not in portable and '胰腺癌' not in portable and 'cancer_ordering_fingerprint' not in portable


def test_selected_pending_candidate_is_independent_and_does_not_carry_whole_original_or_author(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-export-pending')
    _collect(patient, ('病理诊断：右肺上叶浸润性腺癌 pT2aN1M0 ⅢA期。',))
    row = _row(patient)
    snapshot = build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[row['id']]))
    item, = snapshot['cancer_candidates']
    assert item['label'] == '右肺上叶浸润性腺癌'
    assert item['assertion'] == 'AFFIRMED' and item['subject'] == 'CURRENT_PRIMARY'
    assert item['status'] == 'PENDING' and item['value_origin'] == 'REPORT'
    assert item['source'] == {'state': 'OMITTED', 'reason': 'SOURCE_CONTENT_NOT_SELECTED'}
    assert snapshot['documents'] == snapshot['facts'] == snapshot['labs'] == snapshot['sources'] == []
    portable = json_bytes(snapshot).decode()
    assert 'pT2aN1M0' not in portable and 'ⅢA期' not in portable and str(patient.account_id) not in portable


def test_selected_manual_correction_retains_historical_uncertain_meaning_without_old_values(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-export-correction')
    _collect(patient)
    _revise(patient, _row(patient), 'CORRECT', checked_original=True, reason='合成更正',
            changes={'label': '胰腺癌', 'profile': 'PANCREAS', 'assertion': 'UNCERTAIN', 'subject': 'HISTORICAL'})
    snapshot = build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[_row(patient, 'PANCREAS')['id']]))
    item, = snapshot['cancer_candidates']
    assert item['label'] == '胰腺癌' and item['subject'] == 'HISTORICAL' and item['assertion'] == 'UNCERTAIN'
    assert item['value_origin'] == 'MANUAL_CORRECTION'
    assert '肺癌' not in json_bytes(snapshot).decode()


def test_explicit_display_preference_can_be_carried_without_diagnosis_or_candidate_relationship(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-export-preference')
    _collect(patient)
    _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    snapshot = build_snapshot(patient, selected_body(patient, include_indicator_ordering=True))
    choice, = snapshot['indicator_ordering']
    assert choice['mode'] == 'MANUAL_PROFILE' and choice['profile'] == 'PANCREAS'
    assert choice['candidate_id'] is None and choice['reason'] == 'manual_display_preference'
    assert snapshot['cancer_candidates'] == [] and snapshot['documents'] == []


def test_selected_source_reference_requires_the_actual_source_fact_and_document(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-export-source')
    document, version = _collect(patient)
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
    _revise(patient, _row(patient), 'CONFIRM', checked_original=True)
    selection = selected_body(patient, document_ids=[str(document.pk)], fact_ids=[str(fact.pk)],
                              cancer_candidate_ids=[_row(patient)['id']], sections=['diagnosis', 'cancer_ordering'])
    snapshot = build_snapshot(patient, selection)
    source = snapshot['cancer_candidates'][0]['source']
    assert source['state'] == 'SELECTED_REFERENCE' and source['fact_id'] == str(fact.pk)
    assert source['document_id'] == str(document.pk) and source['page'] == fact.document_page.page_number
    selection['fact_ids'] = []
    assert build_snapshot(patient, selection)['cancer_candidates'][0]['source']['state'] == 'OMITTED'


@pytest.mark.parametrize('mode', ['AUTO', 'MANUAL_PROFILE'])
def test_new_uncollected_input_outside_selected_documents_invalidates_and_scrubs_actual_preview(django_user_model, mode):
    client, patient = _patient(django_user_model, 'cancer-export-new-input-' + mode)
    assert client.get('/records/').status_code == 200
    rows = labs(patient)
    _collect(patient)
    if mode == 'MANUAL_PROFILE':
        _select(patient, mode, profile='LUNG')
    selection = {'mode': 'documents', 'document_ids': [str(rows[0].parsing_version.document_id)], 'sections': ['labs']}
    job = create_preview(patient, client.session.session_key, selection, actor=patient.account)
    assert_snapshot_current(patient, job.snapshot)
    parsed_facts(patient, ['出院诊断：胰腺癌。'])
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, job.snapshot)
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending


def test_candidate_body_rejects_an_old_confirmation_guard_and_foreign_candidate(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-export-guard')
    _, other = _patient(django_user_model, 'cancer-export-foreign')
    _collect(patient)
    _collect(other)
    initial = selected_body(patient, cancer_candidate_ids=[_row(patient)['id']])
    _revise(patient, _row(patient), 'DEFER')
    with pytest.raises(ExportInputError):
        build_snapshot(patient, initial)
    with pytest.raises(PermissionDenied):
        build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[_row(other)['id']]))
