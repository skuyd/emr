import json
import uuid

import pytest
from django.urls import reverse

from apps.labs.models import LabReportReviewEvent
from apps.labs.report_workspace import report_workspace
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db
PATH = reverse('labs:report_workspace')


def test_workspace_get_shows_full_report_and_only_its_sources(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-view-get')
    _, first, unit = report(patient)
    report(patient, number='B200', value='9')
    workspace = report_workspace(patient)
    key = next(item['key'] for item in workspace['reports'] if first in item['rows'])

    response = client.get(PATH, {'patient': str(patient.pk), 'report': key})

    assert response.status_code == 200
    assert response.context['current']['key'] == key
    assert first.raw_name in response.content.decode()
    assert '报告核对' in response.content.decode()
    assert len(response.context['current']['sources']) == 1
    assert unit in response.context['current']['units']
    assert response.context['can_write']


def test_workspace_post_saves_one_report_and_returns_navigation(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-view-post')
    _, first, _ = report(patient)
    report(patient, number='B200')
    current = next(item for item in report_workspace(patient)['reports'] if first in item['rows'])
    response = client.post(PATH, {'patient_id': str(patient.pk), 'report_key': current['key'],
        'token': current['token'], 'operation_id': str(uuid.uuid4()), 'intent': 'confirm_next',
        'edits': json.dumps({'observations': [{'id': str(first.pk), 'expected_revision': 0,
            'changes': {'raw_value': '7'}}]})})
    assert response.status_code == 200
    result = response.json()
    assert result['confirmed'] is True and result['next_url']
    assert LabReportReviewEvent.objects.filter(patient=patient, action='CONFIRM').count() == 1


def test_workspace_save_keeps_safe_return_to_document(django_user_model):
    from urllib.parse import parse_qs, urlsplit

    client, patient = _patient(django_user_model, 'workspace-view-return-save')
    document, _, _ = report(patient)
    current = report_workspace(patient)['current']
    target = f'/records/{document.pk}/?patient={patient.pk}'
    response = client.post(PATH + '?return_to=' + target.replace('?', '%3F').replace('&', '%26'), {
        'patient_id': str(patient.pk), 'report_key': current['key'], 'token': current['token'],
        'operation_id': str(uuid.uuid4()), 'intent': 'save', 'edits': '{}'})

    assert response.status_code == 200
    assert parse_qs(urlsplit(response.json()['next_url']).query)['return_to'] == [target]


def test_workspace_rejects_invalid_payload_without_writes(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-view-invalid')
    _, first, _ = report(patient)
    current = report_workspace(patient)['current']
    response = client.post(PATH, {'patient_id': str(patient.pk), 'report_key': current['key'],
        'token': current['token'], 'operation_id': str(uuid.uuid4()), 'intent': 'confirm',
        'edits': '{bad'})
    assert response.status_code == 400
    assert response.json()['error']
    assert not first.revisions.exists() and not LabReportReviewEvent.objects.exists()


def test_workspace_rejects_native_form_post_without_edit_payload(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-view-no-script')
    _, first, _ = report(patient)
    current = report_workspace(patient)['current']

    response = client.post(PATH, {'patient_id': str(patient.pk), 'report_key': current['key'],
        'token': current['token'], 'operation_id': str(uuid.uuid4()), 'intent': 'confirm',
        'raw_value': '7'})

    assert response.status_code == 400
    assert not first.revisions.exists() and not LabReportReviewEvent.objects.exists()


def test_workspace_get_rejects_unknown_report_key(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-view-key')
    report(patient)
    assert client.get(PATH, {'patient': str(patient.pk), 'report': 'other'}).status_code == 404


def test_workspace_empty_state_reports_processing_documents(django_user_model):
    from apps.documents.models import DocumentStatus

    client, patient = _patient(django_user_model, 'workspace-view-processing')
    document, row, unit = report(patient)
    row.delete()
    unit.delete()
    document.status = DocumentStatus.PROCESSING
    document.save(update_fields=['status'])

    response = client.get(PATH, {'patient': str(patient.pk)})

    assert response.status_code == 200
    assert response.context['current'] is None
    assert '1 份资料仍在处理中' in response.content.decode()


def test_viewer_can_read_but_cannot_post_and_cross_patient_cannot_read(django_user_model):
    from apps.patients.models import PatientMembership

    _owner_client, patient = _patient(django_user_model, 'workspace-view-owner')
    viewer_client, viewer_patient = _patient(django_user_model, 'workspace-view-viewer')
    outsider_client, _outsider = _patient(django_user_model, 'workspace-view-outsider')
    PatientMembership.objects.create(patient=patient, account=viewer_patient.account, role='VIEWER')
    report(patient)
    current = report_workspace(patient)['current']
    response = viewer_client.get(PATH, {'patient': str(patient.pk), 'report': current['key']})
    assert response.status_code == 200 and not response.context['can_write']
    assert '确认本报告' not in response.content.decode()
    response = viewer_client.post(PATH, {'patient_id': str(patient.pk), 'report_key': current['key'],
        'token': current['token'], 'operation_id': str(uuid.uuid4()), 'intent': 'confirm', 'edits': '{}'})
    assert response.status_code in {403, 404}
    assert outsider_client.get(PATH, {'patient': str(patient.pk), 'report': current['key']}).status_code == 404
    assert not LabReportReviewEvent.objects.exists()
