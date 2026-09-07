from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import Patient
from apps.self_records.models import DailyRecord
from apps.self_records.services import create_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


def form_data(patient, **changes):
    return {**payload(), 'patient_id': str(patient.pk), 'creation_key': str(uuid4()), **changes}


def test_quick_form_saves_explicit_patient_and_real_actor_after_switch(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-form')
    opened = client.get('/self-records/new/', {'patient': str(patient.pk)})
    assert opened.status_code == 200
    own = Patient.objects.get(account=actor)
    client.post(f'/patients/{own.pk}/select/')
    data = form_data(patient, creation_key=str(opened.context['form']['creation_key'].value()))
    response = client.post('/self-records/new/', data)
    assert response.status_code == 302
    record = DailyRecord.objects.get()
    assert record.patient_id == patient.pk and record.created_by_id == actor.pk
    assert parse_qs(urlsplit(response.url).query)['patient'] == [str(patient.pk)]
    detail = client.get(response.url)
    assert detail.status_code == 200 and detail.context['request'].patient.pk == patient.pk
    assert '60.0 kg' in detail.content.decode()


def test_form_errors_retain_raw_input_without_saving_or_rotating_retry_key(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-error')
    data = form_data(patient, value='>60', notes='保留这次输入')
    response = client.post('/self-records/new/', data)
    assert response.status_code == 400 and not DailyRecord.objects.exists()
    assert response.context['form']['value'].value() == '>60'
    assert response.context['form']['creation_key'].value() == data['creation_key']
    assert response.context['form']['notes'].value() == data['notes']


def test_edit_delete_undo_and_old_revision_conflict_keep_history(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-edit')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    edit_url = f'/self-records/{record.pk}/edit/'
    assert client.get(edit_url).status_code == 200
    changed = client.post(edit_url, form_data(patient, expected_revision='0', value='62'))
    assert changed.status_code == 302
    stale = client.post(edit_url, form_data(patient, expected_revision='0', value='65'))
    assert stale.status_code == 409
    record.refresh_from_db()
    assert record.revision_number == 1 and record.current_data['raw_value'] == '62'
    scope = {'patient_id': str(patient.pk), 'expected_revision': '1'}
    assert client.post(f'/self-records/{record.pk}/delete/', scope).status_code == 302
    assert str(record.pk) not in client.get('/self-records/').content.decode()
    detail = client.get(f'/self-records/{record.pk}/')
    assert detail.status_code == 200 and '已删除' in detail.content.decode()
    scope['expected_revision'] = '2'
    assert client.post(f'/self-records/{record.pk}/undo/', scope).status_code == 302
    record.refresh_from_db()
    assert record.deleted_at is None and record.revisions.count() == 3
    assert record.original_data['raw_value'] == '60.0'


def test_viewer_and_foreign_patient_cannot_mutate_or_read_wrong_record(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-viewer', 'VIEWER')
    record = create_record(patient, patient.account, payload(), creation_key=uuid4()).record
    assert client.get(f'/self-records/{record.pk}/').status_code == 200
    assert client.post('/self-records/new/', form_data(patient)).status_code == 403
    for suffix in ('edit/', 'delete/', 'undo/'):
        assert client.post(f'/self-records/{record.pk}/{suffix}', form_data(patient, expected_revision=0)).status_code == 403
    _, other, foreign, _, _ = family(django_user_model, 'daily-foreign')
    for suffix in ('', 'edit/'):
        assert foreign.get(f'/self-records/{record.pk}/{suffix}', {'patient': str(other.pk)}).status_code == 404
    record.refresh_from_db()
    assert record.revision_number == 0 and record.deleted_at is None


def test_list_filters_use_display_timezone_and_keep_same_minute_records(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-filter')
    first = create_record(patient, actor, payload(measured_local='2026-09-08T00:15'), creation_key=uuid4()).record
    second = create_record(patient, actor, payload(measured_local='2026-09-08T00:15', value='61'), creation_key=uuid4()).record
    create_record(patient, actor, payload(measured_local='2026-09-09T00:15'), creation_key=uuid4())
    response = client.get('/self-records/', {'start': '2026-09-07', 'end': '2026-09-07', 'timezone': 'UTC'})
    assert response.status_code == 200
    assert {row.pk for row in response.context['records']} == {first.pk, second.pk}
    assert '2026-09-07 16:15' in response.content.decode()
    assert client.get('/self-records/', {'start': '2026-09-09', 'end': '2026-09-08'}).status_code == 400


def test_read_rechecks_membership_after_render_and_audits_actual_record(django_user_model, monkeypatch):
    from apps.self_records import views
    _, patient, client, actor, member = family(django_user_model, 'daily-read-race')
    record = create_record(patient, actor, payload(notes='不可泄漏的自记录'), creation_key=uuid4()).record
    original_render = views.render
    def revoke_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        type(member).objects.filter(pk=member.pk).update(revoked_at=timezone.now())
        return response
    monkeypatch.setattr(views, 'render', revoke_after_render)
    response = client.get(f'/self-records/{record.pk}/')
    assert response.status_code in {403, 404}
    assert '不可泄漏的自记录' not in response.content.decode()
    event = AuditEvent.objects.filter(action='self_record_viewed', target_hash=_hash('target', record.pk)).latest('created_at')
    assert event.result == 'denied' and event.actor_hash == _hash('actor', actor.pk)
    assert event.patient_hash == _hash('patient', patient.pk) and event.resource_type == 'self_record'


def test_dst_repeat_can_be_disambiguated_explicitly_in_the_form(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-dst')
    data = form_data(patient, measured_local='2026-10-25T02:30', timezone='Europe/Berlin')
    assert client.post('/self-records/new/', data).status_code == 400
    data['utc_offset'] = '+01:00'
    assert client.post('/self-records/new/', data).status_code == 302
    assert DailyRecord.objects.get().measured_at.isoformat() == '2026-10-25T01:30:00+00:00'
