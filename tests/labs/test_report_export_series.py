import pytest

from tests.labs.helpers import export_series
from apps.labs.comparison import comparison_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def test_export_series_uses_latest_daily_sampling(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, last, _ = report(patient, number='A3', at='2026-09-17 10:30', value='4')
    series, = export_series(patient)
    points = series.points
    assert [point.observation.raw_value for point in points] == ['2', '4']
    assert points[-1].observation.pk == last.pk
    table = comparison_view(patient)
    assert table.result_count == 3


def test_latest_conflict_retains_details_without_a_main_point(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-conflict')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    report(patient, number='A3', at='2026-09-17 10:30', value='4')
    report(patient, number='A4', at='2026-09-17 10:30', value='5')
    assert not export_series(patient)
    assert comparison_view(patient).result_count == 4


def test_export_series_separates_hospitals(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-hospitals')
    for institution in ('甲医院', '乙医院'):
        for day in (16, 17):
            _, _, unit = report(patient, number=f'A{day}', at=f'2026-09-{day} 08:30', value=str(day))
            unit.automatic['institution'] = institution
            unit.save(update_fields=['automatic'])
    series = export_series(patient)
    assert len(series) == 2
    assert {item.points[0].observation.comparison_institution for item in series} == {'甲医院', '乙医院'}


def test_latest_comparator_blocks_earlier_numeric_point(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-comparator')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, row, _ = report(patient, number='A3', at='2026-09-17 10:30', value='<4')
    row.result_type = 'COMPARATOR'
    row.save(update_fields=['result_type'])
    assert not export_series(patient)
    assert comparison_view(patient).result_count == 3


def test_export_series_excludes_disputed_days(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-gap')
    for day in range(14, 20):
        report(patient, number=f'A{day}', at=f'2026-09-{day} 08:30', value=str(day))
    report(patient, number='CONFLICT', at='2026-09-16 08:30', value='20')
    series, = export_series(patient)
    assert [point.observation.observation_date.day for point in series.points] == [14, 15, 17, 18, 19]
