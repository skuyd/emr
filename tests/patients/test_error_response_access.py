"""An invalid form must not release patient context after live access changes."""
from uuid import uuid4

import pytest

from apps.patients.access import change_membership
from tests.patients.test_family_access import family

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('domain', ['self_records', 'glucose'])
@pytest.mark.parametrize('access_change', ['unchanged', 'revoked', 'downgraded', 'inactive'])
def test_invalid_edit_rechecks_live_access_before_releasing_private_context(
        django_user_model, monkeypatch, domain, access_change):
    _, patient, client, actor, member = family(
        django_user_model, 'error-response-' + domain + '-' + access_change)
    private_name = 'SYNTHETIC_SAVED_PATIENT_9341'
    patient.display_name = private_name
    patient.save(update_fields=['display_name'])
    if domain == 'self_records':
        from apps.self_records import views
        from apps.self_records.services import create_record
        from tests.self_records.test_payloads import payload
        record = create_record(patient, patient.account, payload(), creation_key=uuid4()).record
        route = f'/self-records/{record.pk}/edit/'
    else:
        from apps.glucose import views
        from apps.glucose.services import create_record
        from tests.glucose.test_forms import values
        record = create_record(patient, patient.account, values(), creation_key=uuid4()).record
        route = f'/glucose/{record.pk}/edit/'
    assert client.get(route, {'patient': str(patient.pk)}).status_code == 200
    original_render = views.render
    rendered = []

    def change_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        assert response.status_code == 400
        assert private_name.encode() in response.content
        rendered.append(response)
        if access_change == 'revoked':
            change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
        elif access_change == 'downgraded':
            change_membership(patient, patient.account, member.pk, role='VIEWER', expected_revision=0)
        elif access_change == 'inactive':
            type(actor).objects.filter(pk=actor.pk).update(is_active=False)
        return response

    monkeypatch.setattr(views, 'render', change_after_render)
    # The stored private name is deliberately absent from submitted input.
    response = client.post(route, {'patient_id': str(patient.pk), 'notes': 'keep invalid input'})
    assert len(rendered) == 1
    record.refresh_from_db()
    assert record.revision_number == 0 and not record.revisions.exists()
    if access_change == 'unchanged':
        assert response.status_code == 400
        assert private_name.encode() in response.content
        assert response.context['form']['notes'].value() == 'keep invalid input'
    else:
        assert response.status_code in {403, 404}
        assert private_name.encode() not in response.content
        assert rendered[0].closed
