import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.documents.test_detail_viewer import _patient

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('route', ['get', 'invalid_index', 'invalid_detail'])
def test_permission_revoked_during_final_typed_resolution_scrubs_private_body(django_user_model, monkeypatch, route):
    from apps.cancer_ordering import views
    _, patient, _, _, field, _ = typed_fixture(django_user_model, name='review-late-permission-' + route)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    viewer, own = _patient(django_user_model, 'review-late-editor-' + route)
    membership = PatientMembership.objects.create(patient=patient, account=own.account, role='EDITOR')
    real = views.resolve_ordering
    calls = []
    def final_resolution(*args, **kwargs):
        state = real(*args, **kwargs)
        calls.append(state['fingerprint'])
        if len(calls) == 2:
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return state
    monkeypatch.setattr(views, 'resolve_ordering', final_resolution)
    if route == 'get':
        response = viewer.get('/cancer-ordering/', {'patient': str(patient.pk)})
    elif route == 'invalid_index':
        response = viewer.post('/cancer-ordering/', {'patient_id': str(patient.pk), 'mode': 'INVALID'})
    else:
        response = viewer.post('/cancer-ordering/candidates/' + str(candidate.pk) + '/', {'patient_id': str(patient.pk), 'action': 'INVALID'})
    assert len(calls) == 2
    assert response.status_code == 403
    assert '\u80ba\u764c' not in response.content.decode()
