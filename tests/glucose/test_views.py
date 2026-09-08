from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.glucose.models import GlucoseRecord
from apps.glucose.services import create_record, import_lab_record
from apps.glucose.sources import preview_lab
from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import Patient
from tests.glucose.factories import lab_source
from tests.glucose.test_forms import values
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def submitted(patient, **changes):
    return {**values(), 'patient_id': str(patient.pk), **changes}


def test_manual_form_keeps_opened_patient_after_switch_and_saves_actual_actor(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-opened-patient')
    response = client.get('/glucose/new/', {'patient': str(patient.pk)})
    assert response.status_code == 200
    own = Patient.objects.get(account=actor)
    client.post(f'/patients/{own.pk}/select/')
    saved = client.post('/glucose/new/', submitted(patient,
        creation_key=str(response.context['form']['creation_key'].value())))
    assert saved.status_code == 302
    record = GlucoseRecord.objects.get()
    assert record.patient_id == patient.pk and record.created_by_id == actor.pk
    assert parse_qs(urlsplit(saved.url).query)['patient'] == [str(patient.pk)]
    detail = client.get(saved.url)
    assert detail.status_code == 200 and '06:12:34' in detail.content.decode()


def test_invalid_form_keeps_entered_text_and_retry_key_without_saving(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'glucose-invalid-form')
    data = submitted(patient, measured_local='2026-02-30T06:12:34', value=' 8.7 ', notes='本次输入保留')
    response = client.post('/glucose/new/', data)
    assert response.status_code == 400 and not GlucoseRecord.objects.exists()
    for name in ('value', 'creation_key', 'notes', 'measured_local'):
        assert response.context['form'][name].value() == data[name]


def test_edit_delete_and_undo_preserve_original_and_optimistic_revision(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-revisions')
    record = create_record(patient, actor, values(), creation_key=uuid4(), source_kind='METER').record
    prefix = f'/glucose/{record.pk}/'
    assert client.get(prefix + 'edit/').status_code == 200
    assert client.post(prefix + 'edit/', submitted(patient, expected_revision=0, value='8.4')).status_code == 302
    assert client.post(prefix + 'edit/', submitted(patient, expected_revision=0, value='9.1')).status_code == 409
    assert client.post(prefix + 'delete/', {'patient_id': str(patient.pk), 'expected_revision': 1}).status_code == 302
    assert str(record.pk) not in client.get('/glucose/', {'patient': str(patient.pk)}).content.decode()
    assert '已删除' in client.get(prefix).content.decode()
    assert client.post(prefix + 'undo/', {'patient_id': str(patient.pk), 'expected_revision': 2}).status_code == 302
    record.refresh_from_db()
    assert record.deleted_at is None and record.revision_number == 3 and record.revisions.count() == 3
    assert record.original_data['raw_value'] == ' ７.２ ' and record.current_data['raw_value'] == '8.4'


def test_viewer_can_read_but_cannot_mutate_and_foreign_scope_is_rejected(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'glucose-viewer', 'VIEWER')
    record = create_record(patient, patient.account, values(), creation_key=uuid4()).record
    assert client.get(f'/glucose/{record.pk}/').status_code == 200
    assert client.post('/glucose/new/', submitted(patient)).status_code == 403
    for suffix in ('edit/', 'delete/', 'undo/'):
        assert client.post(f'/glucose/{record.pk}/{suffix}', submitted(patient, expected_revision=0)).status_code == 403
    _, other, foreign, _, _ = family(django_user_model, 'glucose-foreign')
    assert foreign.get(f'/glucose/{record.pk}/', {'patient': str(other.pk)}).status_code == 404


def test_history_uses_original_local_day_and_preserves_every_same_time_point(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-history-page')
    records = [create_record(patient, actor, values(value=value, measured_local='2026-08-02T00:12:34'),
        creation_key=uuid4()).record for value in ('7.1', '7.3')]
    create_record(patient, actor, values(measured_local='2026-08-03T00:12:34'), creation_key=uuid4())
    response = client.get('/glucose/', {'patient': str(patient.pk), 'start': '2026-08-02', 'end': '2026-08-02'})
    assert response.status_code == 200
    assert {record.pk for record in response.context['records']} == {record.pk for record in records}
    assert response.context['charts'][0]['points_count'] == 2
    assert response.context['charts'][0]['series'][0]['segments'] == []
    assert client.get('/glucose/', {'start': '2026-08-03', 'end': '2026-08-02'}).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_read_rechecks_membership_and_audits_actual_glucose_record(django_user_model, monkeypatch):
    from apps.glucose import views
    _, patient, client, actor, membership = family(django_user_model, 'glucose-read-revoked')
    record = create_record(patient, actor, values(notes='不得返回的合成备注'), creation_key=uuid4()).record
    original = views.render

    def revoke(*args, **kwargs):
        response = original(*args, **kwargs)
        type(membership).objects.filter(pk=membership.pk).update(revoked_at=timezone.now())
        return response

    monkeypatch.setattr(views, 'render', revoke)
    response = client.get(f'/glucose/{record.pk}/')
    assert response.status_code in (403, 404) and '不得返回的合成备注' not in response.content.decode()
    event = AuditEvent.objects.filter(action='glucose_record_viewed', target_hash=_hash('target', record.pk)).latest('created_at')
    assert event.result == 'denied' and event.resource_type == 'glucose_record'
    assert event.patient_hash == _hash('patient', patient.pk) and event.actor_hash == _hash('actor', actor.pk)


def test_lab_preview_import_and_duplicate_do_not_invent_timezone(django_user_model):
    client, patient, _, _, observation = lab_source(django_user_model)
    url = f'/glucose/import/labs/{observation.pk}/'
    opened = client.get(url)
    assert opened.status_code == 200
    data = {'patient_id': str(patient.pk), 'creation_key': str(opened.context['form']['creation_key'].value()),
        'expected_source': opened.context['form']['expected_source'].value(), 'checked_original': 'on'}
    saved = client.post(url, data)
    assert saved.status_code == 302
    record = GlucoseRecord.objects.get()
    assert record.measured_at is None and record.current_data['local_time'] == '2026-08-02T06:12:34'
    assert client.post(url, data).status_code == 302 and GlucoseRecord.objects.count() == 1
    body = client.get(saved.url).content.decode()
    assert '时区未确认' in body and '原始记录' in body


def test_lab_changed_after_preview_is_rejected_then_rechecked_on_same_uuid(django_user_model):
    from apps.labs.revisions import revise_observation
    client, patient, _, _, observation = lab_source(django_user_model)
    candidate = preview_lab(patient, patient.account, observation.pk)
    record = import_lab_record(patient, patient.account, observation.pk,
        expected_source=candidate['source_fingerprint'], checked_original=True, creation_key=uuid4()).record
    old = client.get(f'/glucose/{record.pk}/recheck/')
    assert old.status_code == 200
    data = {'patient_id': str(patient.pk), 'creation_key': str(old.context['form']['creation_key'].value()),
            'expected_source': old.context['form']['expected_source'].value(), 'expected_revision': 0,
            'checked_original': 'on'}
    revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    assert client.post(f'/glucose/{record.pk}/recheck/', data).status_code == 409
    fresh = client.get(f'/glucose/{record.pk}/recheck/')
    data.update(creation_key=str(fresh.context['form']['creation_key'].value()),
                expected_source=fresh.context['form']['expected_source'].value())
    assert client.post(f'/glucose/{record.pk}/recheck/', data).status_code == 302
    record.refresh_from_db()
    assert record.revision_number == 1 and record.current_data['raw_value'] == '8.40'
    assert record.original_data['raw_value'] == '8.20' and GlucoseRecord.objects.count() == 1


def test_nursing_summary_is_retained_without_chart_point(django_user_model):
    client, patient, document, _, observation = lab_source(django_user_model)
    url = f'/glucose/import/nursing/{document.pk}/{observation.document_page.page_number}/'
    opened = client.get(url)
    assert opened.status_code == 200
    data = submitted(patient, source_kind='NURSING', value='7.0–9.0', measured_local='', time_precision='UNKNOWN',
        timezone='', original_excerpt='近期血糖7.0–9.0 mmol/L', measurement_scope='SUMMARY', checked_original='on',
        creation_key=str(opened.context['form']['creation_key'].value()),
        expected_source=opened.context['form']['expected_source'].value())
    saved = client.post(url, data)
    assert saved.status_code == 302
    record = GlucoseRecord.objects.get()
    assert record.source_page_id == observation.document_page_id and not record.current_data['plot_eligible']
    history = client.get('/glucose/', {'patient': str(patient.pk), 'date_scope': 'UNKNOWN'})
    assert history.status_code == 200 and history.context['charts'] == []
    assert '7.0–9.0' in history.content.decode()


def test_active_authors_are_distinguished_from_deleted_accounts(django_user_model):
    from apps.glucose.services import revise_record
    owner, patient, client, actor, _ = family(django_user_model, 'glucose-visible-author')
    record = create_record(patient, actor, values(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=values(value='8.1'))
    own_body = client.get(f'/glucose/{record.pk}/').content.decode()
    assert own_body.count('本人') >= 2 and '已注销' not in own_body
    owner_body = owner.get(f'/glucose/{record.pk}/').content.decode()
    assert owner_body.count(str(actor.pk)) >= 2 and '已注销' not in owner_body


@pytest.mark.parametrize('page_name', ['index', 'detail', 'edit'])
@pytest.mark.parametrize('change', ['revision', 'source'])
@pytest.mark.django_db(transaction=True)
def test_record_reads_discard_content_changed_during_render(django_user_model, monkeypatch, page_name, change):
    from apps.glucose import views
    from apps.glucose.services import revise_record
    from apps.labs.revisions import revise_observation
    client, patient, _, _, observation = lab_source(django_user_model)
    candidate = preview_lab(patient, patient.account, observation.pk)
    record = import_lab_record(patient, patient.account, observation.pk,
        expected_source=candidate['source_fingerprint'], checked_original=True, creation_key=uuid4(),
        confirm_timezone=True, timezone_name='Asia/Shanghai').record
    original = views.render

    def change_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        if change == 'source':
            revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_value': '9.19'}, expected_revision=0)
        else:
            revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0,
                changes=values(value='9.19'))
        return response

    monkeypatch.setattr(views, 'render', change_after_render)
    url = {'index': '/glucose/', 'detail': f'/glucose/{record.pk}/', 'edit': f'/glucose/{record.pk}/edit/'}[page_name]
    response = client.get(url, {'patient': str(patient.pk)})
    assert response.status_code == 409 and '8.20' not in response.content.decode()
    assert {'no-store', 'private'} <= {part.strip() for part in response['Cache-Control'].split(',')}


@pytest.mark.parametrize('page_name', ['lab', 'nursing', 'sources'])
@pytest.mark.django_db(transaction=True)
def test_source_previews_discard_content_when_original_is_removed_during_render(django_user_model, monkeypatch, page_name):
    from apps.glucose import views
    client, patient, document, _, observation = lab_source(django_user_model)
    original = views.render

    def remove_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        type(document).objects.filter(pk=document.pk).update(deleted_at=timezone.now())
        return response

    monkeypatch.setattr(views, 'render', remove_after_render)
    url = {'lab': f'/glucose/import/labs/{observation.pk}/',
           'nursing': f'/glucose/import/nursing/{document.pk}/1/', 'sources': '/glucose/sources/'}[page_name]
    response = client.get(url, {'patient': str(patient.pk)})
    assert response.status_code == 409 and str(document.pk) not in response.content.decode()
    assert '8.20' not in response.content.decode()


def test_changed_source_remains_visible_as_stale_and_is_removed_from_both_charts(django_user_model):
    from apps.labs.revisions import revise_observation
    client, patient, _, _, observation = lab_source(django_user_model)
    candidate = preview_lab(patient, patient.account, observation.pk)
    record = import_lab_record(patient, patient.account, observation.pk,
        expected_source=candidate['source_fingerprint'], checked_original=True, creation_key=uuid4(),
        confirm_timezone=True, timezone_name='Asia/Shanghai').record
    assert client.get('/glucose/').context['charts'][0]['points_count'] == 1
    revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_value': '9.19'}, expected_revision=0)
    response = client.get('/glucose/')
    assert response.status_code == 200 and response.context['charts'] == [] and response.context['heatmaps'] == []
    assert str(record.pk) in response.content.decode() and '来源' in response.context['rows'][0]['status']
    body = client.get(f'/glucose/{record.pk}/').content.decode()
    assert '8.20' in body and '重新核对来源' in body


def test_history_pagination_declares_the_displayed_scope_and_keeps_filters(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-history-pages')
    created = {create_record(patient, actor, values(value='7.2', measured_local=f'2026-08-02T06:{minute:02}:34'),
        source_kind='METER', creation_key=uuid4()).record.pk for minute in range(51)}
    query = {'patient': str(patient.pk), 'source_kind': 'METER', 'start': '2026-08-02'}
    first = client.get('/glucose/', query)
    second = client.get('/glucose/', {**query, 'page': 2})
    assert first.status_code == second.status_code == 200
    assert first.context['page'].paginator.count == 51 and len(first.context['records']) == 50
    assert len(second.context['records']) == 1
    assert {row.pk for page in (first, second) for row in page.context['records']} == created
    assert first.context['charts'][0]['points_count'] == 50 and second.context['charts'][0]['points_count'] == 1
    assert '仅使用本页记录' in first.content.decode() and first.context['filters_active']
    assert parse_qs(first.context['filter_query']) == {key: [value] for key, value in query.items()}
