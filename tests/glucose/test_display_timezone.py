from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.glucose.history import display_row, history_charts
from apps.glucose.payloads import normalize_payload
from apps.glucose.services import create_record, import_lab_record
from apps.glucose.sources import preview_lab
from tests.glucose.factories import lab_source
from tests.glucose.test_forms import values
from tests.patients.test_family_access import family


def projected(local, *, precision='SECOND', zone='Asia/Shanghai', display='UTC'):
    data = normalize_payload({'value': '7.2', 'unit': 'mmol/L', 'measured_local': local,
        'time_precision': precision, 'timezone': zone,
        'timezone_origin': 'USER_CONFIRMED' if zone else 'UNCONFIRMED',
        'time_slot': 'FASTING', 'source_label': '合成设备', 'notes': ''},
        source_kind='LAB_REPORT', allow_imprecise=True)
    record = SimpleNamespace(pk=uuid4(), current_data=data, source_kind='LAB_REPORT', deleted_at=None)
    before = deepcopy(data)
    result = display_row(record, display_timezone=display)
    assert data == before and result['data'] == before
    return result


def test_display_timezone_moves_known_seconds_to_same_day_in_curve_and_heatmap():
    row = projected('2026-08-02T00:30:12')
    assert row['time_label'] == '2026-08-01 16:30:12'
    assert row['date_label'] == '2026-08-01' and row['time_converted']
    assert row['original_time_label'] == '2026-08-02 00:30:12'
    assert row['timezone_label'].startswith('UTC')
    charts = history_charts([row])
    point = charts['charts'][0]['series'][0]['points'][0]
    assert point['day'] == charts['heatmaps'][0]['days'][0]['day'] == '2026-08-01'
    assert point['seconds'] == 16 * 3600 + 30 * 60 + 12


def test_selected_utc_keeps_two_dst_occurrences_as_distinct_actual_times():
    rows = [projected('2026-10-25T02:30:12' + offset, zone='Europe/Berlin') for offset in ('+02:00', '+01:00')]
    chart = history_charts(rows)['charts'][0]
    assert [row['time_label'] for row in rows] == ['2026-10-25 00:30:12', '2026-10-25 01:30:12']
    assert chart['points_count'] == 2
    assert all(point['same_time_count'] == 1 for point in chart['series'][0]['points'])


@pytest.mark.parametrize('local,precision,zone', [
    ('2026-08-02T00:30:12', 'SECOND', ''), ('2026-08-02', 'DAY', 'Asia/Shanghai'),
    ('2026-08', 'MONTH', 'Asia/Shanghai'), ('2026', 'YEAR', 'Asia/Shanghai'), ('', 'UNKNOWN', ''),
])
def test_uncertain_instant_retains_original_precision_and_day(local, precision, zone):
    row = projected(local, precision=precision, zone=zone)
    assert not row['time_converted'] and row['conversion_note']
    assert row['time_label'] == (local.replace('T', ' ') if local else '时间不详')
    assert row['date_label'] == (row['data']['measured_date'] or '日期不详')
    assert history_charts([row])['charts'] == []


@pytest.mark.django_db
def test_history_filters_and_sorts_by_selected_display_day_and_keeps_original_storage(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-display-day')
    earlier = create_record(patient, actor, values(measured_local='2026-08-02T00:30:12'), creation_key=uuid4()).record
    later = create_record(patient, actor, values(measured_local='2026-08-02T09:30:12'), creation_key=uuid4()).record
    selected = {'patient': str(patient.pk), 'display_timezone': 'UTC', 'start': '2026-08-01', 'end': '2026-08-01'}
    response = client.get('/glucose/', selected)
    assert response.status_code == 200 and [record.pk for record in response.context['records']] == [earlier.pk]
    assert response.context['rows'][0]['time_label'] == '2026-08-01 16:30:12'
    assert '原记录：2026-08-02 00:30:12' in response.content.decode()
    assert 'display_timezone=UTC' in response.context['filter_query']
    earlier.refresh_from_db()
    assert earlier.current_data['local_time'] == '2026-08-02T00:30:12' and earlier.revision_number == 0
    next_day = client.get('/glucose/', {**selected, 'start': '2026-08-02', 'end': '2026-08-02'})
    assert [record.pk for record in next_day.context['records']] == [later.pk]


@pytest.mark.django_db
def test_display_filter_keeps_unconfirmed_report_and_date_only_on_original_day(django_user_model):
    client, patient, _, _, timed = lab_source(django_user_model, marker='glucose-display-unknown')
    _, _, _, _, dated = lab_source(django_user_model, patient=patient, sample='2026-08-02')
    for observation in (timed, dated):
        candidate = preview_lab(patient, patient.account, observation.pk)
        import_lab_record(patient, patient.account, observation.pk,
            expected_source=candidate['source_fingerprint'], checked_original=True, creation_key=uuid4())
    response = client.get('/glucose/', {'display_timezone': 'Pacific/Honolulu', 'start': '2026-08-02', 'end': '2026-08-02'})
    assert response.status_code == 200 and len(response.context['records']) == 2
    assert response.context['charts'] == []
    assert all(not row['time_converted'] and row['date_label'] == '2026-08-02' for row in response.context['rows'])
    assert '没有确定时刻' in response.content.decode()


@pytest.mark.django_db
def test_invalid_display_timezone_is_a_retained_filter_error(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-display-invalid')
    record = create_record(patient, actor, values(), creation_key=uuid4()).record
    response = client.get('/glucose/', {'display_timezone': 'Mars/Olympus', 'patient': str(patient.pk)})
    assert response.status_code == 400 and response.context['rows'] == []
    assert response.context['form']['display_timezone'].value() == 'Mars/Olympus'
    assert 'display_timezone' in response.context['form'].errors
    assert record.revision_number == 0


@pytest.mark.django_db
@pytest.mark.parametrize('local,display', [('0001-01-01T00:00:00', 'Etc/GMT+1'), ('9999-12-31T23:59:59', 'Etc/GMT-1')])
def test_display_conversion_outside_calendar_range_is_a_filter_error(django_user_model, local, display):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-display-calendar-' + local[:4])
    record = create_record(patient, actor, values(measured_local=local, timezone='UTC'), creation_key=uuid4()).record
    response = client.get('/glucose/', {'display_timezone': display, 'patient': str(patient.pk)})
    assert response.status_code == 400 and 'display_timezone' in response.context['form'].errors
    assert '清除筛选' in response.content.decode()
    record.refresh_from_db()
    assert record.current_data['local_time'] == local and record.revision_number == 0
