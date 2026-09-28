import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.labs.models import LabConfirmationBatch, LabReportReviewEvent, LabReportRevision, ObservationRevision
from apps.labs.report_workspace import report_workspace
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def test_old_read_routes_open_same_report_workspace(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-old-get')
    _, row, unit = report(patient)
    key = report_workspace(patient)['current']['key']
    for path in [reverse('labs:batch_confirmation'), reverse('labs:observation', args=[row.pk]),
                 reverse('labs:report_detail', args=[unit.pk])]:
        response = client.get(path, {'patient': str(patient.pk)})
        assert response.status_code == 302
        assert reverse('labs:report_workspace') in response['Location']
        if path != reverse('labs:batch_confirmation'):
            assert parse_qs(urlsplit(response['Location']).query)['report'] == [key]


def test_old_posts_do_not_modify_content(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-old-post')
    _, row, unit = report(patient)
    cases = [
        (reverse('labs:batch_confirmation'), {'report': ['fake'], 'operation_id': str(uuid.uuid4())}),
        (reverse('labs:observation', args=[row.pk]), {'action': 'CONFIRM', 'expected_revision': 0}),
        (reverse('labs:report_detail', args=[unit.pk]), {'field': 'institution', 'value': '伪造医院',
            'expected_revision': 0, 'operation_id': str(uuid.uuid4())}),
    ]
    for path, payload in cases:
        response = client.post(path, {'patient_id': str(patient.pk), **payload})
        assert response.status_code == 410
        assert '报告核对' in response.content.decode()
    assert not ObservationRevision.objects.exists()
    assert not LabReportRevision.objects.exists()
    assert not LabConfirmationBatch.objects.exists()
    assert not LabReportReviewEvent.objects.exists()


def test_old_get_only_accepts_safe_patient_return(django_user_model):
    client, patient = _patient(django_user_model, 'workspace-old-return')
    _, row, _ = report(patient)
    path = reverse('labs:observation', args=[row.pk])
    allowed = reverse('labs:comparison') + '?patient=' + str(patient.pk) + '&project=LAB_WBC'
    response = client.get(path, {'patient': str(patient.pk), 'return_to': allowed})
    assert 'return_to=' in response['Location']
    redirected = client.get(response['Location'])
    assert redirected.context['return_to'] == allowed + f'#result-{row.pk}'
    response = client.get(path, {'patient': str(patient.pk), 'return_to': 'https://evil.example/'})
    assert 'return_to=' not in response['Location']


def test_historical_observation_link_selects_its_report_after_materialization(django_user_model):
    from datetime import date
    from tests.labs.test_trends import _observation

    client, patient = _patient(django_user_model, 'workspace-historical-focus')
    _observation(patient, date(2026, 8, 1), '5')
    _, target = _observation(patient, date(2026, 9, 1), '7')

    response = client.get(reverse('labs:observation', args=[target.pk]), {'patient': str(patient.pk)}, follow=True)

    assert response.status_code == 200
    assert response.context['focus_observation'] == str(target.pk)
    assert target.pk in {row.pk for row in response.context['current']['rows']}
