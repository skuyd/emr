from datetime import date
from decimal import Decimal

import pytest

from apps.labs.comparison import comparison_view
from apps.labs.revisions import revise_observation
from apps.labs.trends import trend_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def observations(patient, values=('2', '4', '6', '12'), **kwargs):
    return [_observation(patient, date(2026, 8, i + 1), value, **kwargs)[1] for i, value in enumerate(values)]


def cells(view):
    return [cell for row in view.rows for entries in row.cells for cell in entries]


def test_effective_comparison_and_trend_share_the_same_baseline_and_sources(django_user_model):
    _, patient = _patient(django_user_model, 'personal-baseline')
    rows = observations(patient)
    table = cells(comparison_view(patient))[-1].change
    chart = trend_view(patient, 'LAB_WBC').series[0].points[-1].change
    for result in (table, chart):
        assert result.baseline_mean == Decimal('4') and result.baseline_percentage == Decimal('200')
        assert [c.observation.pk for c in result.baseline] == [row.pk for row in rows[:3]]
        assert result.previous.observation.pk == rows[-2].pk
        assert result.previous_percentage == Decimal('100')


def test_date_filter_keeps_referenced_prior_baseline_outside_visible_columns(django_user_model):
    _, patient = _patient(django_user_model, 'filtered-baseline')
    observations(patient)
    view = comparison_view(patient, start=date(2026, 8, 4))
    assert len(view.columns) == 1
    assert cells(view)[0].change.baseline_mean == Decimal('4')
    assert len(cells(view)[0].change.baseline) == 3


def test_correction_recomputes_baseline_and_reported_error_removes_its_eligibility(django_user_model):
    _, patient = _patient(django_user_model, 'revised-baseline')
    rows = observations(patient)
    revise_observation(patient.account, rows[0].pk, action='CORRECT', changes={'raw_value': '8'}, expected_revision=0)
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean == Decimal('6') and change.baseline_percentage == Decimal('100')
    assert rows[0].raw_value == '2'
    revise_observation(patient.account, rows[0].pk, action='REPORT_ERROR', changes={}, expected_revision=1)
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean is None and change.baseline_reason


def test_actual_quality_and_method_exclusions_do_not_supply_three_point_baseline(django_user_model):
    _, patient = _patient(django_user_model, 'quality-baseline')
    rows = observations(patient)
    rows[0].evidence.confidence = Decimal('0.2')
    rows[0].evidence.save(update_fields=['confidence'])
    _observation(patient, date(2026, 8, 2), '999', method='合成方法B')
    current = next(cell for cell in cells(comparison_view(patient)) if cell.observation.pk == rows[-1].pk)
    assert current.change.baseline_mean is None
    assert current.change.previous_percentage == Decimal('100')


def test_tumor_markers_use_fifty_percent_descriptive_boundary_from_dictionary(django_user_model):
    _, patient = _patient(django_user_model, 'tumor-baseline')
    observations(patient, ('4', '4', '4', '6'), code='LAB_CEA', standard_name='癌胚抗原', raw_name='CEA', raw_unit='ng/mL')
    current = cells(comparison_view(patient))[-1]
    assert current.change.threshold_percent == 50
    assert current.change.baseline_percentage == Decimal('50') and not current.change.highlight


def test_removed_document_cannot_remain_a_baseline_source(django_user_model):
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'removed-baseline')
    rows = observations(patient)
    document = rows[0].parsing_version.document
    document.deleted_at = timezone.now()
    document.save(update_fields=['deleted_at'])
    assert cells(comparison_view(patient))[-1].change.baseline_mean is None


def test_comparison_exposes_grouped_rows_and_sparkline_points_without_losing_cells(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-sparkline')
    rows = observations(patient)
    view = comparison_view(patient)
    assert sum(len(group.rows) for group in view.groups) == len(view.rows)
    assert [point.observation.pk for point in view.rows[0].sparkline] == [row.pk for row in rows]
    assert len(cells(view)) == 4


def test_multi_indicator_get_has_independent_units_and_patient_scoped_sources(django_user_model):
    client, patient = _patient(django_user_model, 'multi-indicator')
    _, other = _patient(django_user_model, 'multi-indicator-other')
    rows = observations(patient)
    observations(patient, ('100', '120', '110', '130'), code='LAB_HGB', raw_unit='g/L', standard_name='血红蛋白')
    hidden = observations(other, ('900', '999'))
    response = client.get('/trends/compare/', {'code': ['LAB_WBC', 'LAB_HGB'], 'start': '2026-08-01', 'end': '2026-08-04'})
    assert response.status_code == 200
    content = response.content.decode()
    assert '10^9/L' in content and 'g/L' in content
    assert all(str(row.pk) in content for row in rows)
    assert all(str(row.pk) not in content for row in hidden)
    assert '各图使用独立数轴' in content


def test_multi_indicator_filter_validation_and_single_available_point_remain_explicit(django_user_model):
    client, patient = _patient(django_user_model, 'multi-filter')
    observations(patient)
    response = client.get('/trends/compare/', {'code': ['LAB_WBC'], 'start': '2026-09-01', 'end': '2026-08-01'})
    assert response.status_code == 400
    response = client.get('/trends/compare/', {'code': ['LAB_WBC'], 'start': '2026-08-04'})
    assert response.status_code == 200 and '不足两个不同日期的可比结果' in response.content.decode()
