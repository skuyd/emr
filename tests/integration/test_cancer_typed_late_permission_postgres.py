"""Real committed revocation after final source reads, before private release."""
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.documents.test_detail_viewer import _patient
from tests.integration.test_cancer_narrative_output_postgres import committed, require_postgresql

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('route', ['index', 'detail', 'export', 'share'])
def test_committed_final_permission_change_never_releases_private_error_body(
        request, django_user_model, monkeypatch, route, changed):
    from apps.cancer_ordering import output_forms, views
    _, patient, _, _, field, _ = typed_fixture(django_user_model, name='typed-committed-' + route)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    client, own = _patient(django_user_model, 'typed-committed-admin-' + route)
    membership = PatientMembership.objects.create(patient=patient, account=own.account, role='ADMIN')
    module = views if route in {'index', 'detail'} else output_forms
    original = module.resolve_ordering
    calls = []

    def commit_after_final_source_read(*args, **kwargs):
        state = original(*args, **kwargs)
        calls.append(state['fingerprint'])
        if len(calls) == 2:
            committed(request, 'typed_final_permission', lambda: change_membership(
                patient, patient.account, membership.pk, revoke=True, expected_revision=0) if changed else None)
        return state

    monkeypatch.setattr(module, 'resolve_ordering', commit_after_final_source_read)
    paths = {'index': '/cancer-ordering/', 'detail': f'/cancer-ordering/candidates/{candidate.pk}/',
             'export': '/visit/', 'share': f'/patients/{patient.pk}/shares/'}
    response = client.post(paths[route], {'patient_id': str(patient.pk), 'mode': 'INVALID', 'action': 'INVALID'})
    assert len(calls) == 2
    assert response.status_code == (403 if changed else 400)
    assert ('肺癌' in response.content.decode()) is not changed
    revoked = committed(request, 'typed_permission_readback', lambda: PatientMembership.objects.get(pk=membership.pk).revoked_at)
    assert (revoked is not None) is changed
    response.close()
