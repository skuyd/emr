from datetime import date
from decimal import Decimal

import pytest

from apps.labs.revisions import revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.helpers import _observation, export_series


pytestmark = pytest.mark.django_db


def test_export_series_preserves_raw_values_and_numeric_precision(django_user_model):
    _, patient = _patient(django_user_model, 'series-values')
    _, first = _observation(patient, date(2026, 7, 1), '4.200')
    _, second = _observation(patient, date(2026, 8, 20), '5.0', raw_name='WBC')
    series, = export_series(patient)
    assert [(point.observation.pk, point.numeric_value) for point in series.points] == [
        (first.pk, Decimal('4.200')), (second.pk, Decimal('5.0'))]
    assert [point.observation.raw_value for point in series.points] == ['4.200', '5.0']
    assert series.unit == '10^9/L'
    assert '合成检验中心' in series.basis_label and '合成方法A' in series.basis_label


def test_export_series_resolves_revision_before_eligibility(django_user_model):
    _, patient = _patient(django_user_model, 'series-revision')
    _observation(patient, date(2026, 7, 1), '4.2')
    _, row = _observation(patient, date(2026, 8, 1), '4.6')
    revise_observation(patient.account, row.pk, action='REPORT_ERROR', changes={}, expected_revision=0)
    assert not export_series(patient)


@pytest.mark.parametrize('first_overrides,second_overrides', [
    ({}, {'result_type': 'COMPARATOR'}),
    ({'precision': 'MONTH'}, {'precision': 'MONTH'}),
    ({'raw_unit': 'mg/L'}, {'raw_unit': 'mmol/L'}),
    ({'method': '方法A'}, {'method': '方法B'}),
    ({'method': ''}, {'method': ''}),
    ({'capability': 'SEARCH_ONLY'}, {'capability': 'SEARCH_ONLY'}),
])
def test_ineligible_combinations_do_not_enter_export_series(django_user_model, first_overrides, second_overrides):
    _, patient = _patient(django_user_model, 'series-ineligible')
    _observation(patient, date(2026, 7, 1), '4.2', **first_overrides)
    _observation(patient, date(2026, 8, 1), '4.6', **second_overrides)
    assert not export_series(patient)


def test_export_series_does_not_mix_other_patient_records(django_user_model):
    _, patient = _patient(django_user_model, 'series-owner')
    _, other = _patient(django_user_model, 'series-other')
    _observation(patient, date(2026, 7, 1), '4.2')
    _observation(other, date(2026, 7, 1), '99.1')
    _observation(other, date(2026, 8, 1), '99.2')
    assert not export_series(patient)
    assert [point.numeric_value for series in export_series(other) for point in series.points] == [Decimal('99.1'), Decimal('99.2')]
