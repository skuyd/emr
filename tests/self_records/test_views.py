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
from tests.self_records.test_daily_entry_views import form_data
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


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
    assert client.get(response.url).status_code == 200


def test_form_errors_retain_raw_input_without_saving_or_rotating_retry_key(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-error')
    data = form_data(patient, value='>60')
    response = client.post('/self-records/new/', data)
    assert response.status_code == 400 and not DailyRecord.objects.exists()
    assert response.context['form']['value'].value() == '>60'
    assert response.context['form']['creation_key'].value() == data['creation_key']
    assert '本次尚未保存' in response.content.decode()


def test_stale_correction_cannot_overwrite_newer_value(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-stale-edit')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    url = f'/self-records/{record.pk}/edit/'
    changed = client.post(url, form_data(patient, expected_revision='0', value='62'))
    assert changed.status_code == 302
    stale = client.post(url, form_data(patient, expected_revision='0', value='65'))
    assert stale.status_code == 409
    record.refresh_from_db()
    assert record.revision_number == 1 and record.current_data['raw_value'] == '62'
    assert not record.revisions.exists()


def test_viewer_and_foreign_patient_cannot_mutate_or_read_wrong_record(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-viewer', 'VIEWER')
    record = create_record(patient, patient.account, payload(), creation_key=uuid4()).record
    assert client.get(f'/self-records/{record.pk}/').status_code == 200
    assert client.post('/self-records/new/', form_data(patient)).status_code == 403
    for suffix in ('edit/', 'delete/'):
        assert client.post(f'/self-records/{record.pk}/{suffix}',
                           form_data(patient, expected_revision=0, confirm='delete')).status_code == 403
    _, other, foreign, _, _ = family(django_user_model, 'daily-foreign')
    for suffix in ('', 'edit/'):
        assert foreign.get(f'/self-records/{record.pk}/{suffix}',
                           {'patient': str(other.pk)}).status_code == 404
    record.refresh_from_db()
    assert record.revision_number == 0 and record.deleted_at is None


def test_read_rechecks_membership_after_render_and_audits_actual_record(django_user_model, monkeypatch):
    from apps.self_records import views
    _, patient, client, actor, member = family(django_user_model, 'daily-read-race')
    record = create_record(patient, actor, payload(value='765432'), creation_key=uuid4()).record
    original_render = views.render

    def revoke_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        type(member).objects.filter(pk=member.pk).update(revoked_at=timezone.now())
        return response

    monkeypatch.setattr(views, 'render', revoke_after_render)
    response = client.get(f'/self-records/{record.pk}/')
    assert response.status_code in {403, 404}
    assert '765432' not in response.content.decode()
    event = AuditEvent.objects.filter(action='self_record_viewed',
                                      target_hash=_hash('target', record.pk)).latest('created_at')
    assert event.result == 'denied' and event.actor_hash == _hash('actor', actor.pk)
    assert event.patient_hash == _hash('patient', patient.pk) and event.resource_type == 'self_record'


def test_dst_repeated_minute_is_saved_without_offset_and_not_shifted_on_edit(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-dst')
    response = client.post('/self-records/new/', form_data(
        patient, measured_local='2026-10-25T02:30'))
    assert response.status_code == 302
    record = DailyRecord.objects.get()
    assert record.current_data['local_time'] == '2026-10-25T02:30'
    assert record.measured_at is None
    edited = client.post(f'/self-records/{record.pk}/edit/', form_data(
        patient, measured_local='2026-10-25T02:30', value='62', expected_revision='0'))
    assert edited.status_code == 302
    record.refresh_from_db()
    assert record.current_data['local_time'] == '2026-10-25T02:30'
    assert record.current_data['raw_value'] == '62' and record.measured_at is None
