from uuid import uuid4

import pytest

from apps.self_records.models import DailyRecord
from apps.self_records.services import create_record
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def form_data(patient, **changes):
    return {'patient_id': str(patient.pk), 'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30',
            'value': '60', 'unit': 'kg', 'creation_key': str(uuid4()), **changes}


def test_create_stays_on_entry_form_and_can_save_two_independent_entries(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-entry-again')
    first = client.post('/self-records/new/', form_data(patient))
    assert first.status_code == 302 and '/self-records/new/' in first.url
    assert client.get(first.url).status_code == 200
    second = client.post('/self-records/new/', form_data(patient, value='61'))
    assert second.status_code == 302 and '/self-records/new/' in second.url
    assert sorted(row.current_data['raw_value'] for row in DailyRecord.objects.filter(patient=patient)) == ['60', '61']


def test_existing_endpoint_filters_selected_patient_date_and_kind(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-existing')
    chosen = create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30',
                                            'value': '60', 'unit': 'kg'}, creation_key=uuid4()).record
    create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-01', 'score': 0},
                  creation_key=uuid4())
    response = client.get('/self-records/existing/', {'patient': str(patient.pk), 'date': '2026-10-01',
                                                       'kind': 'WEIGHT'})
    assert response.status_code == 200
    assert [row['id'] for row in response.json()['records']] == [str(chosen.pk)]
    assert response.json()['records'][0]['time'] == '08:30'
    assert client.get('/self-records/existing/', {'patient': str(patient.pk), 'date': 'bad',
                                                  'kind': 'WEIGHT'}).status_code == 400


def test_edit_changes_same_row_then_returns_to_add_state_without_revision_history(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-edit-view')
    record = create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30',
                                            'value': '60', 'unit': 'kg'}, creation_key=uuid4()).record
    url = f'/self-records/{record.pk}/edit/'
    opened = client.get(url, {'patient': str(patient.pk)})
    assert opened.status_code == 200 and 'name="expected_revision" value="0"' in opened.content.decode()
    changed = client.post(url, form_data(patient, expected_revision='0', value='61'))
    assert changed.status_code == 302 and '/self-records/new/' in changed.url
    record.refresh_from_db()
    assert record.current_data['raw_value'] == '61' and not record.revisions.exists()
    forbidden = client.post(url, form_data(patient, kind='ECOG', record_date='2026-10-01',
                                           score='0', expected_revision='1'))
    assert forbidden.status_code == 400 and record.current_data['raw_value'] == '61'


def test_delete_requires_confirmation_and_undo_route_is_gone(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-confirm-view')
    record = create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30',
                                            'value': '60', 'unit': 'kg'}, creation_key=uuid4()).record
    url = f'/self-records/{record.pk}/delete/'
    scope = {'patient_id': str(patient.pk), 'expected_revision': '0'}
    assert client.post(url, scope).status_code == 400
    record.refresh_from_db()
    assert record.deleted_at is None
    confirmed = client.post(url, {**scope, 'confirm': 'delete'}, HTTP_ACCEPT='application/json')
    assert confirmed.status_code == 200 and confirmed.json()['deleted'] is True
    assert client.get(f'/self-records/{record.pk}/', {'patient': str(patient.pk)}).status_code == 404
    assert client.post(f'/self-records/{record.pk}/undo/', {**scope, 'confirm': 'delete'}).status_code == 404


def test_invalid_saved_reference_does_not_break_new_form(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'daily-bad-saved')
    response = client.get('/self-records/new/', {'patient': str(patient.pk), 'saved': 'not-a-uuid'})
    assert response.status_code == 200
