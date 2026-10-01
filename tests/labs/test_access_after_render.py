from datetime import date

import pytest

from tests.labs.helpers import _observation


pytestmark = pytest.mark.django_db


def observations(patient):
    return [_observation(patient, date(2026, 8, day), value)[1]
            for day, value in ((1, '2'), (2, '4'), (3, '6'), (4, '12'))]


@pytest.mark.parametrize('module_name,build_name,path', [
    ('apps.labs.views', 'comparison_view', '/labs/compare/'),
])
def test_comparison_rechecks_membership_after_building_the_view(django_user_model, monkeypatch, module_name, build_name, path):
    from importlib import import_module
    from apps.patients.access import change_membership
    from tests.patients.test_family_access import family
    module = import_module(module_name)
    _, patient, client, _, membership = family(django_user_model, 'joint-revoke', 'VIEWER')
    observations(patient)
    original = getattr(module, build_name)
    def revoke_after_build(*args, **kwargs):
        value = original(*args, **kwargs)
        change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return value
    monkeypatch.setattr(module, build_name, revoke_after_build)
    response = client.get(path, {'patient': str(patient.pk), 'code': 'LAB_WBC'})
    assert response.status_code == 403
    assert 'comparison-table' not in response.content.decode()



def test_readonly_member_sees_comparison_but_cannot_post_revision(django_user_model):
    from tests.patients.test_family_access import family
    _, patient, client, _, _ = family(django_user_model, 'joint-readonly', 'VIEWER')
    rows = observations(patient)
    assert client.get('/labs/compare/', {'patient': str(patient.pk)}).status_code == 200
    assert client.post(f'/labs/observations/{rows[0].pk}/', {'patient_id': str(patient.pk), 'action': 'CORRECT'}).status_code == 403
    rows[0].refresh_from_db()
    assert rows[0].raw_value == '2' and rows[0].revision_number == 0
