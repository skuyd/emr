from datetime import date, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest


def cell(number, value, *, day=None, patient='patient-a', key=('LAB_WBC', 'BLOOD', '10^9/L', 'method-a', 'trusted'), eligible=True, threshold=30):
    observation = SimpleNamespace(
        pk=f'{patient}-{number}', observation_date=day or date(2026, 8, 1) + timedelta(days=number),
        parsing_version=SimpleNamespace(document=SimpleNamespace(patient_id=patient)),
    )
    return SimpleNamespace(observation=observation, group_key=key, trend_eligible=eligible,
                           numeric_value=Decimal(value), unit='10^9/L', change_threshold_percent=threshold)


def result(cells, current=-1):
    from apps.labs.change_metrics import changes_for_cells
    return changes_for_cells(cells)[str(cells[current].observation.pk)]


def test_previous_three_exclude_current_and_future_and_rate_uses_actual_days():
    history = [cell(1, '2'), cell(2, '4'), cell(3, '6'), cell(13, '12'), cell(14, '100')]
    change = result(history, 3)
    assert change.baseline_mean == Decimal('4')
    assert change.baseline_percentage == Decimal('200')
    assert change.previous_percentage == Decimal('100')
    assert change.absolute_change == Decimal('6')
    assert change.daily_change == Decimal('0.6')
    assert change.elapsed_days == 10
    assert [c.observation.pk for c in change.baseline] == [c.observation.pk for c in history[:3]]
    assert change.previous.observation.pk == history[2].observation.pk
    assert change.highlight


@pytest.mark.parametrize('baseline', ['0', '-1'])
def test_nonpositive_baseline_keeps_values_and_absolute_change_but_has_no_percent(baseline):
    history = [cell(1, baseline), cell(2, baseline), cell(3, baseline), cell(4, '2')]
    change = result(history)
    assert change.baseline_mean == Decimal(baseline)
    assert change.previous_percentage is None and change.baseline_percentage is None
    assert change.absolute_change == Decimal('2') - Decimal(baseline)
    assert change.daily_change == change.absolute_change
    assert change.previous_reason and change.baseline_reason
    assert not change.highlight


def test_insufficient_history_explains_missing_mean_without_hiding_previous_change():
    change = result([cell(1, '3'), cell(4, '6')])
    assert change.previous_percentage == Decimal('100') and change.daily_change == Decimal('1')
    assert change.baseline_mean is None and change.baseline_percentage is None
    assert change.baseline_reason and not change.highlight


@pytest.mark.parametrize('duplicate_current', [False, True])
def test_duplicate_dates_do_not_create_arbitrary_percentages(duplicate_current):
    history = [cell(1, '2'), cell(2, '4'), cell(3, '6'), cell(4, '8')]
    duplicate = cell(9, '10', day=history[-1 if duplicate_current else -2].observation.observation_date)
    change = result([*history, duplicate], 3)
    assert change.previous_percentage is None and change.baseline_percentage is None
    assert change.daily_change is None
    assert change.previous_reason and change.baseline_reason


def test_duplicate_date_anywhere_in_last_three_is_not_replaced_by_older_values():
    history = [cell(1, '2'), cell(2, '4'), cell(3, '6', day=date(2026, 8, 3)), cell(4, '8'), cell(5, '10')]
    change = result(history)
    assert change.previous_percentage == Decimal('25')
    assert change.baseline_mean is None and change.baseline_reason


def test_groups_patients_and_untrusted_rows_cannot_supply_baselines():
    history = [cell(1, '2'), cell(2, '4'), cell(3, '6'), cell(10, '8')]
    unrelated = [cell(8, '99', patient='patient-b'), cell(7, '99', key=('LAB_WBC', 'BLOOD', '10^9/L', 'method-b', 'trusted')), cell(9, '99', eligible=False)]
    change = result([*history, *unrelated], 3)
    assert change.baseline_mean == Decimal('4') and change.previous.observation.pk == history[2].observation.pk
    uncertain = result([*history, *unrelated])
    assert uncertain.previous_percentage is None and uncertain.baseline_percentage is None
    assert uncertain.previous_reason and not uncertain.highlight


@pytest.mark.parametrize('value,threshold,highlight', [('13', 30, False), ('13.01', 30, True), ('15', 50, False), ('15.01', 50, True), ('4.99', 50, True)])
def test_descriptive_threshold_is_strict_and_specific_to_indicator_group(value, threshold, highlight):
    history = [cell(1, '10', threshold=threshold), cell(2, '10', threshold=threshold), cell(3, '10', threshold=threshold), cell(4, value, threshold=threshold)]
    change = result(history)
    assert change.highlight is highlight
    assert change.threshold_percent == threshold


def test_calculation_does_not_use_callers_decimal_precision():
    history = [cell(1, '1'), cell(2, '2'), cell(3, '3'), cell(4, '7')]
    with localcontext() as context:
        context.prec = 2
        context.Emax, context.Emin = 2, -2
        change = result(history)
    assert change.baseline_mean == Decimal('2') and change.baseline_percentage == Decimal('250')
    assert change.absolute_change == Decimal('4')


def test_extreme_unsupported_derived_values_are_missing_and_never_infinite():
    change = result([cell(1, '1e-1000'), cell(2, '1e-1000'), cell(3, '1e-1000'), cell(4, '1e1000')])
    assert change.previous_percentage is None and change.baseline_percentage is None
    assert change.previous_reason and change.baseline_reason
    assert not change.highlight
