from datetime import date
from urllib.parse import parse_qs, urlsplit

from django.utils import timezone
from freezegun import freeze_time
import pytest

from apps.documents.archive import records_context
from apps.documents.models import Document, DocumentStatus
from apps.processing.models import DatePrecision, DocumentType
from tests.documents.test_records import _patient, _record


pytestmark = pytest.mark.django_db


def _cells(context):
    return {cell['date']: cell for week in context['calendar_weeks'] for cell in week}


def test_calendar_is_default_and_groups_the_whole_month_before_day_pagination(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-month')
    for index in range(21):
        _record(patient, f'day-{index}.pdf', document_date=date(2026, 10, 2), precision=DatePrecision.DAY)
    _record(patient, 'other-day.pdf', document_date=date(2026, 10, 3), precision=DatePrecision.DAY)
    with freeze_time('2026-10-02 04:00:00'):
        context = records_context(patient, {})
    assert context['view_mode'] == 'calendar'
    assert context['calendar_month'] == date(2026, 10, 1)
    assert context['selected_day'] == date(2026, 10, 2)
    assert context['month_count'] == 22
    cells = _cells(context)
    assert cells[date(2026, 10, 2)]['count'] == 21
    assert cells[date(2026, 10, 3)]['count'] == 1
    assert cells[date(2026, 10, 2)]['today']
    assert cells[date(2026, 10, 2)]['selected']
    assert len(context['day_cards']) == 20
    assert context['day_page_obj'].paginator.count == 21
    parameters = {key: values[0] for key, values in parse_qs(urlsplit(context['day_next_url']).query).items()}
    second = records_context(patient, parameters)
    assert len(second['day_cards']) == 1
    assert _cells(second)[date(2026, 10, 2)]['count'] == 21


def test_incomplete_dates_are_separate_and_not_assigned_to_a_placeholder_day(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-incomplete')
    unknown = _record(patient, 'unknown.pdf')
    month = _record(patient, 'month.pdf', document_date=date(2026, 10, 1), precision=DatePrecision.MONTH)
    year = _record(patient, 'year.pdf', document_date=date(2026, 1, 1), precision=DatePrecision.YEAR)
    _record(patient, 'different-month.pdf', document_date=date(2026, 9, 1), precision=DatePrecision.MONTH)
    _record(patient, 'different-year.pdf', document_date=date(2025, 1, 1), precision=DatePrecision.YEAR)
    context = records_context(patient, {'year': '2026', 'month': '10'})
    assert context['month_count'] == 0
    assert all(cell['count'] == 0 for cell in _cells(context).values())
    assert {card.document.pk for card in context['undated_cards']} == {unknown.pk, month.pk, year.pk}


def test_filters_deletions_and_patient_scope_apply_to_counts_and_day_cards(django_user_model):
    client, patient = _patient(django_user_model, 'calendar-filter')
    _, other = _patient(django_user_model, 'calendar-other')
    options = dict(document_date=date(2026, 10, 2), precision=DatePrecision.DAY,
                   document_type=DocumentType.LAB, status=DocumentStatus.ORIGINAL_ONLY)
    visible = _record(patient, 'target.pdf', **options)
    _record(other, 'target-private.pdf', **options)
    deleted = _record(patient, 'target-deleted.pdf', **options)
    Document.objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())
    _record(patient, 'different.pdf', **options)
    _record(patient, 'target-other-type.pdf', document_date=date(2026, 10, 2), precision=DatePrecision.DAY)
    context = records_context(patient, {'q': 'target', 'type': 'LAB', 'status': 'ORIGINAL_ONLY',
                                        'year': '2026', 'month': '10', 'date': '2026-10-02'})
    assert context['month_count'] == 1
    assert _cells(context)[date(2026, 10, 2)]['count'] == 1
    assert [card.document.pk for card in context['day_cards']] == [visible.pk]
    assert context['undated_page_obj'].paginator.count == 0
    for key in ('previous_month_url', 'next_month_url', 'today_url', 'list_url'):
        values = parse_qs(urlsplit(context[key]).query)
        assert values['patient'] == [str(patient.pk)]
        assert values['q'] == ['target']
        assert values['type'] == ['LAB']
        assert values['status'] == ['ORIGINAL_ONLY']
    assert client.get('/records/', {'patient': other.pk, 'view': 'calendar'}).status_code == 404


@pytest.mark.parametrize('parameters', [
    {'year': 'no', 'month': '99', 'date': '2026-02-30', 'view': 'bad'},
    {'year': '0', 'month': '0', 'date': '9999-12-31'},
    {'date': '20261002'},
])
def test_malformed_calendar_parameters_fall_back_to_today(django_user_model, parameters):
    _, patient = _patient(django_user_model, 'calendar-invalid')
    with freeze_time('2026-10-02 04:00:00'):
        context = records_context(patient, parameters)
    assert context['view_mode'] == 'calendar'
    assert context['calendar_month'] == date(2026, 10, 1)
    assert context['selected_day'] == date(2026, 10, 2)


def test_leap_month_adjacent_days_and_year_boundaries(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-bounds')
    leap = records_context(patient, {'year': '2024', 'month': '2', 'date': '2024-02-29'})
    assert len(_cells(leap)) == 35
    assert _cells(leap)[date(2024, 2, 29)]['selected']
    assert not _cells(leap)[date(2024, 3, 1)]['in_month']
    next_values = parse_qs(urlsplit(_cells(leap)[date(2024, 3, 1)]['url']).query)
    assert next_values['month'] == ['3']
    assert next_values['date'] == ['2024-03-01']
    assert records_context(patient, {'year': '1900', 'month': '1'})['previous_month_url'] == ''
    assert records_context(patient, {'year': '2100', 'month': '12'})['next_month_url'] == ''


def test_unknown_date_pagination_preserves_selected_day_and_calendar_counts(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-undated-page')
    for index in range(21):
        _record(patient, f'unknown-{index}.pdf')
        _record(patient, f'dated-{index}.pdf', document_date=date(2026, 10, 3), precision=DatePrecision.DAY)
    context = records_context(patient, {'year': '2026', 'month': '10', 'date': '2026-10-03', 'page': '2'})
    assert len(context['undated_cards']) == 20
    parameters = {key: values[0] for key, values in parse_qs(urlsplit(context['undated_next_url']).query).items()}
    second = records_context(patient, parameters)
    assert second['selected_day'] == date(2026, 10, 3)
    assert len(second['undated_cards']) == 1
    assert second['day_page_obj'].number == 2
    assert len(second['day_cards']) == 1


def test_switching_through_list_keeps_selected_calendar_day(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-roundtrip')
    calendar = records_context(patient, {'year': '2026', 'month': '9', 'date': '2026-09-17'})
    parameters = {key: values[0] for key, values in parse_qs(urlsplit(calendar['list_url']).query).items()}
    listing = records_context(patient, parameters)
    parameters = {key: values[0] for key, values in parse_qs(urlsplit(listing['calendar_url']).query).items()}
    returned = records_context(patient, parameters)
    assert returned['selected_day'] == date(2026, 9, 17)


def test_explicit_list_preserves_all_months_and_links_keep_list_mode(django_user_model):
    _, patient = _patient(django_user_model, 'calendar-list')
    _record(patient, 'october.pdf', document_date=date(2026, 10, 2), precision=DatePrecision.DAY)
    _record(patient, 'september.pdf', document_date=date(2026, 9, 2), precision=DatePrecision.DAY)
    context = records_context(patient, {'view': 'list'})
    assert context['view_mode'] == 'list'
    assert context['page_obj'].paginator.count == 2
    assert parse_qs(context['pagination_query'])['view'] == ['list']
    assert parse_qs(urlsplit(context['clear_url']).query)['view'] == ['list']


def test_global_search_shows_historical_matches_without_overriding_calendar_selection(django_user_model):
    client, patient = _patient(django_user_model, 'calendar-global-search')
    historical = _record(patient, 'historical-report.pdf', document_date=date(2019, 1, 2),
                         precision=DatePrecision.DAY)
    with freeze_time('2026-10-02 04:00:00'):
        response = client.get('/records/', {'q': 'historical'})
        assert response.context['view_mode'] == 'list'
        assert f'/records/{historical.pk}/' in response.content.decode()
        calendar = records_context(patient, {'q': 'historical', 'view': 'calendar'})
        assert calendar['view_mode'] == 'calendar'
        assert calendar['calendar_month'] == date(2026, 10, 1)
        assert calendar['month_count'] == 0
        dated = records_context(patient, {'q': 'historical', 'year': '2019', 'month': '1',
                                         'date': '2019-01-02'})
        assert dated['view_mode'] == 'calendar'
        assert [card.document.pk for card in dated['day_cards']] == [historical.pk]
