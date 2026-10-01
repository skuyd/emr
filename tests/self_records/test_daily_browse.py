from uuid import uuid4

import pytest
from django.utils import timezone

from apps.self_records.services import create_record
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def test_calendar_and_list_share_month_kind_and_keep_same_day_entries(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-browse')
    for value in ('60', '61'):
        create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-25T02:30',
                                       'value': value, 'unit': 'kg'}, creation_key=uuid4())
    create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-25', 'score': 0},
                  creation_key=uuid4())
    query = {'patient': str(patient.pk), 'month': '2026-10', 'date': '2026-10-25', 'kind': 'WEIGHT'}
    calendar = client.get('/self-records/', query)
    assert calendar.status_code == 200
    assert calendar.context['view_mode'] == 'calendar'
    assert calendar.context['month_count'] == 2
    assert len(calendar.context['day_rows']) == 2
    assert '60 kg' in calendar.content.decode() and '61 kg' in calendar.content.decode()
    assert '0 分' not in calendar.content.decode()
    listing = client.get('/self-records/', {**query, 'view': 'list'})
    assert listing.status_code == 200 and listing.context['view_mode'] == 'list'
    assert listing.context['month_count'] == 2
    assert len(listing.context['list_groups'][0]['rows']) == 2
    assert '筛选记录类型、日期与时区' not in listing.content.decode()


def test_month_navigation_crosses_year_and_ecog_displays_no_time(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-year')
    create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-12-31', 'score': 5},
                  creation_key=uuid4())
    response = client.get('/self-records/', {'patient': str(patient.pk), 'month': '2026-12',
                                             'date': '2026-12-31', 'kind': 'ECOG'})
    assert response.status_code == 200
    assert response.context['previous_month'] == '2026-11'
    assert response.context['next_month'] == '2027-01'
    assert response.context['day_rows'][0]['display_time'] == '2026-12-31'
    html = response.content.decode()
    assert '5 分' in html and '00:00' not in html
    assert '上个月' in html and '下个月' in html


def test_detail_exposes_only_effective_content_without_revision_or_undo(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-detail-current')
    record = create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30',
                                            'value': '60', 'unit': 'kg'}, creation_key=uuid4()).record
    response = client.get(f'/self-records/{record.pk}/', {'patient': str(patient.pk)})
    assert response.status_code == 200
    html = response.content.decode()
    assert '60 kg' in html and '2026-10-01' in html
    assert '修订历史' not in html and '首次记录' not in html and '撤销' not in html
    assert 'UTC' not in html and '时区' not in html


@pytest.mark.parametrize('month,today', [('0000-01', '2026-10-01'), ('9999-12', '2026-10-01'),
                                         ('2026-10', '9999-12-31')])
def test_invalid_calendar_boundaries_fall_back_to_current_month(django_user_model, month, today):
    _, patient, client, _, _ = family(django_user_model, 'daily-boundary-' + month + today)
    response = client.get('/self-records/', {'patient': str(patient.pk), 'month': month, 'today': today})
    assert response.status_code == 200
    expected = '2026-10' if today == '2026-10-01' else timezone.localdate().strftime('%Y-%m')
    assert response.context['month_key'] == expected
