import pytest

from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from tests.facts.molecular_factories import graph

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('route', ['field', 'report'])
@pytest.mark.parametrize('post', [False, True])
@pytest.mark.parametrize('change', ['revoke', 'trash'])
def test_final_source_reread_does_not_leave_a_private_response_after_access_changes(django_user_model, monkeypatch, route, post, change):
    from apps.facts import pathology_views
    from apps.documents.lifecycle import move_to_trash
    _, patient, document, report, fields = graph(django_user_model)
    client, collaborator = _patient(django_user_model, 'molecular-final-access')
    member = PatientMembership.objects.create(patient=patient, account=collaborator.account, role='EDITOR')
    name = 'report_material' if route == 'field' else 'review_reports'
    original = getattr(pathology_views, name)
    observed = []
    def reread(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append('READ')
        if len(observed) == 2:
            if change == 'revoke':
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            else:
                move_to_trash(patient, document.pk, actor=patient.account)
            observed.append('CHANGED')
        return result
    monkeypatch.setattr(pathology_views, name, reread)
    url = f'/facts/{fields["identity"].pk}/' if route == 'field' else f'/facts/reports/{report.pk}/'
    response = client.post(url, {'patient_id': str(patient.pk), 'action': 'CONFIRM'}) if post else client.get(url)
    assert observed == ['READ', 'READ', 'CHANGED']
    assert 'NM_SYN.2' not in response.content.decode()
    assert response.status_code == (403 if change == 'revoke' else 404)
