import pytest
from datetime import date

from apps.labs.trends import trend_view
from apps.labs.trends import joint_trend_views
from apps.labs.comparison import comparison_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def test_trend_and_change_summary_use_latest_daily_sampling(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, last, _ = report(patient, number='A3', at='2026-09-17 10:30', value='4')
    view = trend_view(patient, 'LAB_WBC', include_history=True)
    assert len(view.series) == 1
    points = view.series[0].points
    assert [point.observation.raw_value for point in points] == ['2', '4']
    assert points[-1].observation.pk == last.pk
    assert points[-1].change.absolute_change == 2
    assert len(view.daily_details) == 3


def test_latest_conflict_retains_details_without_a_main_point(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-conflict')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    report(patient, number='A3', at='2026-09-17 10:30', value='4')
    report(patient, number='A4', at='2026-09-17 10:30', value='5')
    view = trend_view(patient, 'LAB_WBC', include_history=True)
    assert [point.observation.raw_value for series in view.series for point in series.points] == ['2']
    assert len(view.disputed) == 3
    assert len(view.daily_details) == 4


def test_trend_separates_hospitals(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-hospitals')
    for institution in ('甲医院', '乙医院'):
        for day in (16, 17):
            _, _, unit = report(patient, number=f'A{day}', at=f'2026-09-{day} 08:30', value=str(day))
            unit.automatic['institution'] = institution
            unit.save(update_fields=['automatic'])
    view = trend_view(patient, 'LAB_WBC')
    assert len(view.series) == 2
    assert {series.points[0].observation.comparison_institution for series in view.series} == {'甲医院', '乙医院'}


def test_latest_comparator_blocks_earlier_numeric_point(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-comparator')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, row, _ = report(patient, number='A3', at='2026-09-17 10:30', value='<4')
    row.result_type = 'COMPARATOR'
    row.save(update_fields=['result_type'])
    view = trend_view(patient, 'LAB_WBC', include_history=True)
    assert [point.observation.raw_value for series in view.series for point in series.points] == ['2']
    assert len(view.daily_details) == 3


def test_disputed_day_breaks_lines_and_joint_range_filters_source_details(django_user_model):
    _, patient = _patient(django_user_model, 'sampling-trend-gap')
    for day in range(14, 20):
        report(patient, number=f'A{day}', at=f'2026-09-{day} 08:30', value=str(day))
    report(patient, number='CONFLICT', at='2026-09-16 08:30', value='20')
    view = trend_view(patient, 'LAB_WBC')
    assert [point.observation.observation_date.day for point in view.series[0].points] == [14, 15, 17, 18, 19]
    assert [len(segment.split()) for segment in view.series[0].segments] == [2, 3]
    table = comparison_view(patient)
    assert [len(segment.split()) for segment in table.rows[0].sparkline_segments] == [2, 3]
    joint, bounds = joint_trend_views(patient, ['LAB_WBC'], start=date(2026, 9, 15), end=date(2026, 9, 18))
    assert bounds == (date(2026, 9, 15), date(2026, 9, 18))
    assert [len(segment.split()) for segment in joint[0].series[0].segments] == [2]
    assert len(joint[0].daily_details) == 5
    assert all(15 <= cell.observation.observation_date.day <= 18 for cell in joint[0].daily_details)
