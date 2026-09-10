"""Scope replacement is a real original-review flow, including selected ranges."""
import pytest
from django.test import Client

from apps.facts.models import LateralityScopeBinding, LateralityScopeOperation
from apps.facts.readmodels import effective_fact
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_laterality_scope_field_views import form_data
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def _client(patient):
    client = Client()
    client.force_login(patient.account)
    return client


def _payload(response, patient, source, *, text='双肺门', code='BILATERAL', action='REPLACE', mode='OCR'):
    data = form_data(response)
    data.update(patient_id=str(patient.pk), action=action, checked_original='on', confirm='on',
                scope_kind='WHOLE_ENTITY' if action == 'ATTEST' else 'NAMED_MEMBERS_ONLY',
                **{'members-TOTAL_FORMS': '1', 'members-INITIAL_FORMS': str(response.context['formset'].initial_form_count()), 'members-MIN_NUM_FORMS': '1',
                   'members-MAX_NUM_FORMS': '32', 'members-0-site_text': text, 'members-0-code': code,
                   'members-0-raw_text': text, 'members-0-source_mode': mode, 'members-0-sources': [str(source.pk)]})
    return data


def test_actual_scope_page_replaces_and_undoes_with_sources_and_distinct_history(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'scope-route-replace')
    confirm(patient, parent)
    confirm(patient, child)
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200
    assert '纵隔及双肺门' in page.content.decode() and '原文' in page.content.decode()
    response = client.post(url, _payload(page, patient, parent.source_fragments.get()))
    assert response.status_code == 302
    operation = LateralityScopeOperation.objects.get(action='REPLACE')
    detail = client.get(response['Location'])
    assert detail.status_code == 200 and str(operation.original_old_id) in detail.content.decode()
    assert effective_fact(operation.new_fact)['usable']
    reversal = client.post(response['Location'], {'patient_id': str(patient.pk), 'action': 'UNDO',
                                                'expected_operation': detail.context['operation']['current_token']})
    assert reversal.status_code == 302
    child.refresh_from_db()
    assert effective_fact(child)['status'] == 'PENDING'
    assert effective_fact(operation.new_fact)['status'] == 'EXCLUDED'


def test_scope_route_rejects_changed_parent_and_shows_posted_member_text(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'scope-route-stale')
    confirm(patient, parent)
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200
    values = _payload(page, patient, parent.source_fragments.get())
    confirm(patient, parent)
    response = client.post(url, values)
    assert response.status_code == 409
    assert '双肺门' in response.content.decode() and LateralityScopeOperation.objects.count() == 0


def test_independent_whole_attestation_route_keeps_old_field_unknown_on_undo(django_user_model):
    from tests.facts.test_imaging_quantitative import imaging
    _, patient, document, _, _ = imaging(django_user_model, '左肺见结节，长径12mm。', name='scope-route-attest')
    parent = document.facts.get(field_key='lesion.site')
    child = document.facts.get(field_key='lesion.laterality')
    LateralityScopeBinding.objects.filter(fact=child).delete()
    confirm(patient, parent)
    confirm(patient, child)
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200
    values = _payload(page, patient, parent.source_fragments.get(), text='左肺', code='LEFT', action='ATTEST')
    response = client.post(url, values)
    assert response.status_code == 302
    event = LateralityScopeOperation.objects.get(action='ATTEST')
    assert effective_fact(event.new_fact)['laterality_scope']['scope_state'] == 'WHOLE_ENTITY'
    child.refresh_from_db()
    assert effective_fact(child)['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'


def test_scope_creation_route_from_manual_parent_has_no_fake_ocr(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import create_manual_report, add_manual_clinical_field
    from tests.documents.test_detail_viewer import _patient, _document
    _, patient = _patient(django_user_model, 'scope-route-manual')
    document, _ = _document(patient, status='PROCESSING_FAILED')
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk,
        spans=[{'page_number': 1}], title='合成影像', expected_lifecycle_revision=document.lifecycle_revision,
        expected_version_id=None)
    parent = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key='lesion:manual',
        field_key='lesion.site', value={'text': '纵隔及双肺门'}, fragments=[{'page_number': 1, 'raw_text': '纵隔及双肺门'}],
        expected_report_source=report_source_token(report))
    confirm(patient, parent)
    client = _client(patient)
    url = f'/facts/{parent.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200
    response = client.post(url, _payload(page, patient, parent.source_fragments.get(), action='ADD', mode='MANUAL_PAGE'))
    assert response.status_code == 302
    event = LateralityScopeOperation.objects.get(action='ADD')
    assert event.new_fact.source_fragments.get().ocr_block_id is None
    assert event.new_fact.laterality_scope_binding.ranges.get().source_kind == 'MANUAL_PAGE'


@pytest.mark.parametrize('manual', [False, True])
def test_multiple_members_keep_independent_sources_in_form_and_service(django_user_model, manual):
    from tests.facts.test_imaging_quantitative import imaging
    _, patient, document, _, _ = imaging(django_user_model, '左肺及右肾见结节，长径12mm。', name='scope-route-multiple-' + str(manual))
    parent = document.facts.get(field_key='lesion.site')
    child = document.facts.get(field_key='lesion.scoped_laterality')
    confirm(patient, parent)
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200
    source = parent.source_fragments.get()
    mode = 'MANUAL_PAGE' if manual else 'OCR'
    values = _payload(page, patient, source, text='左肺', code='LEFT', mode=mode)
    values.update(**{'members-TOTAL_FORMS': '2', 'members-1-site_text': '右肾', 'members-1-code': 'RIGHT',
                     'members-1-raw_text': '右肾', 'members-1-source_mode': mode, 'members-1-sources': [str(source.pk)]})
    response = client.post(url, values)
    assert response.status_code == 302
    new = LateralityScopeOperation.objects.get(action='REPLACE').new_fact
    assert [(m['site_text'], m['code']) for m in effective_fact(new)['content']['value']['members']] == [('左肺', 'LEFT'), ('右肾', 'RIGHT')]
    assert new.source_fragments.count() == 2


def test_original_cross_block_unicode_selection_keeps_each_actual_block_interval(django_user_model):
    from tests.facts.test_clinical_foundation import clinical_fixture
    texts = ['CT诊断报告书', '影像表现：Ⅲ、纵隔及双', '肺门见淋巴结，短径１２ｍｍ。', '诊断意见：请核对原件。']
    _, patient, document, version, _ = clinical_fixture(django_user_model, texts=texts, name='scope-ui-unicode')
    parent = document.facts.get(field_key='lesion.site')
    child = document.facts.get(field_key='lesion.scoped_laterality')
    confirm(patient, parent)
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    sources = list(parent.source_fragments.order_by('ordinal'))
    values = _payload(page, patient, sources[0], text='双\n肺门')
    values['members-0-sources'] = [str(source.pk) for source in sources]
    response = client.post(url, values)
    assert response.status_code == 302
    new = LateralityScopeOperation.objects.get(action='REPLACE').new_fact
    ranges = list(new.laterality_scope_binding.ranges.order_by('ordinal'))
    assert [(r.reading_order, r.start_offset, r.end_offset, r.raw_text) for r in ranges] == [
        (1, len(texts[1])-1, len(texts[1]), '双'), (2, 0, 2, '肺门')]
    assert list(version.ocr_blocks.order_by('reading_order').values_list('text', flat=True)) == texts


def test_whole_attestation_keeps_old_coded_transcription_distinct_from_new_scope_quote(django_user_model):
    from tests.facts.test_imaging_quantitative import imaging
    _, patient, document, _, _ = imaging(django_user_model, '左肺上叶见结节，长径12mm。', name='scope-route-legacy-short-raw')
    parent = document.facts.get(field_key='lesion.site')
    child = document.facts.get(field_key='lesion.laterality')
    LateralityScopeBinding.objects.filter(fact=child).delete()
    confirm(patient, parent)
    confirm(patient, child, 'CORRECT', changes={'value': {'code': 'LEFT', 'raw': '左侧'}, 'raw_value': '左侧'})
    client = _client(patient)
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    response = client.post(url, _payload(page, patient, parent.source_fragments.get(), text='左肺上叶', code='LEFT', action='ATTEST'))
    assert response.status_code == 302
    new = LateralityScopeOperation.objects.get(action='ATTEST').new_fact
    assert new.automatic_content['value'] == {'code': 'LEFT', 'raw': '左侧'}
    assert new.raw_text == '左肺上叶' and effective_fact(new)['usable']


def test_route_resolution_and_denied_audit_use_actual_resource_patient(django_user_model):
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent
    from apps.patients.models import Patient, PatientMembership
    from tests.documents.test_detail_viewer import _patient
    patient, _, parent, child = fixture(django_user_model, 'scope-route-patient-audit')
    confirm(patient, parent)
    other = Patient.objects.create(account=patient.account, display_name='另一个合成患者')
    client = _client(patient)
    session = client.session
    session['active_patient_id'] = str(other.pk)
    session.save()
    url = f'/facts/{child.pk}/scope/'
    page = client.get(url)
    assert page.status_code == 200 and page.context['request'].patient.pk == patient.pk
    denied = client.get(url + f'?patient={other.pk}')
    assert denied.status_code == 404
    event = AuditEvent.objects.filter(action='fact_viewed', result='denied').latest('created_at')
    assert event.patient_hash == _hash('patient', patient.pk)
    _, reader_patient = _patient(django_user_model, 'scope-route-viewer')
    PatientMembership.objects.create(patient=patient, account=reader_patient.account, role='VIEWER')
    viewer = _client(reader_patient)
    page = viewer.get(url)
    assert page.status_code == 200
    assert viewer.post(url, _payload(page, patient, parent.source_fragments.get())).status_code == 403
    assert not LateralityScopeOperation.objects.exists()


def test_overlapping_occurrences_are_not_silently_resolved_to_first():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from django.core.exceptions import ValidationError
    from apps.facts.laterality_forms import selected_ranges
    parent = Mock()
    source = SimpleNamespace(pk=1, raw_text='aaa', source_kind='OCR', ocr_block_id='synthetic-block', start_offset=7)
    parent.source_fragments.select_related.return_value.order_by.return_value = [source]
    with pytest.raises(ValidationError):
        selected_ranges(parent, 'member:001', {'sources': ['1'], 'raw_text': 'aa', 'source_mode': 'OCR'})
