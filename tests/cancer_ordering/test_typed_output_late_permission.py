import pytest

from apps.cancer_ordering.services import collect_current
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.documents.test_detail_viewer import _patient

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('kind', ['export', 'share'])
def test_actual_output_form_rechecks_permission_after_final_source_read(django_user_model, monkeypatch, kind):
    from apps.cancer_ordering import output_forms
    _, patient, _, _, _, _ = typed_fixture(django_user_model, name='typed-output-permission-' + kind)
    collect_current(patient, actor=patient.account)
    viewer, own = _patient(django_user_model, 'typed-output-editor-' + kind)
    membership = PatientMembership.objects.create(patient=patient, account=own.account, role='ADMIN')
    original = output_forms.resolve_ordering
    calls = []

    def revoke_after_state(*args, **kwargs):
        state = original(*args, **kwargs)
        calls.append(state['fingerprint'])
        if len(calls) == 2:
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return state

    monkeypatch.setattr(output_forms, 'resolve_ordering', revoke_after_state)
    path = '/visit/' if kind == 'export' else f'/patients/{patient.pk}/shares/'
    response = viewer.post(path, {'patient_id': str(patient.pk), 'mode': 'INVALID'})
    assert len(calls) == 2
    assert response.status_code == 403
    assert '肺癌' not in response.content.decode()
