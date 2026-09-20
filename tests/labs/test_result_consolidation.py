from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from apps.labs.comparison import ComparisonCell
from apps.labs.comparison_policy import AbnormalResult
from apps.labs.consolidation import fold_cells, latest_daily_cells
from tests.labs.test_report_identity import unit


def cell(identity, value='5', *, at='2026-09-17 08:30', institution='合成医院',
         raw_unit='10^9/L', result_type='NUMERIC', code='LAB_WBC', specimen='BLOOD',
         reference='1-10', flag='', issues=(), patient='p1', method='method-a'):
    report = replace(unit(f'采样时间：{at}'), institution=institution)
    observation = SimpleNamespace(pk=identity, standard_code=code, specimen=specimen,
        result_type=result_type, raw_value=value, raw_unit=raw_unit, quality_issues=issues,
        reference_range_raw=reference, report_flag_raw=flag, report_identity=report,
        observation_date=report.sampled_at.date(), comparison_institution=institution,
        parsing_version=SimpleNamespace(document=SimpleNamespace(patient_id=patient), document_id=identity),
        evidence=SimpleNamespace(confidence=.99))
    return ComparisonCell(observation, 'direct', '', True, None, raw_unit, None, issues, '',
        (code, specimen, raw_unit, method, 'trusted'), plot_eligible=True,
        abnormal=AbnormalResult('above' if flag else 'within'), known_unit=bool(raw_unit))


def test_equal_numeric_formats_fold_but_keep_every_real_sampling_source():
    groups = fold_cells((cell('a', '5.0'), cell('b', '5.00', at='2026-09-17 10:30')))
    assert len(groups) == 1
    assert [row.pk for row in groups[0].sources] == ['a', 'b']
    assert [row.raw_value for row in groups[0].sources] == ['5.0', '5.00']
    assert groups[0].latest_sampled_at == datetime(2026, 9, 17, 10, 30)


@pytest.mark.parametrize('first,second', [
    (cell('a', '5.0000000000000001'), cell('b', '5.0000000000000002')),
    (cell('a', '<5', result_type='COMPARATOR'), cell('b', '5')),
    (cell('a', '<5', result_type='COMPARATOR'), cell('b', '≤5', result_type='COMPARATOR')),
    (cell('a', '阴性', result_type='QUALITATIVE'), cell('b', 'negative', result_type='QUALITATIVE')),
    (cell('a'), cell('b', raw_unit='10^12/L')),
    (cell('a'), cell('b', institution='其他医院')),
    (cell('a'), cell('b', institution='')),
    (cell('a', institution=''), cell('b', institution='')),
    (cell('a'), cell('b', patient='p2')),
    (cell('a'), cell('b', at='2026-09-18 08:30')),
    (cell('a'), cell('b', specimen='URINE')),
    (cell('a', specimen=''), cell('b', specimen='')),
    (cell('a', code='CANDIDATE_ALB'), cell('b', code='CANDIDATE_ALB')),
    (cell('a', raw_unit=''), cell('b', raw_unit='')),
    (cell('a'), cell('b', issues=({'code': 'association_conflict', 'fields': ['raw_name']},))),
    (cell('a'), cell('b', issues=({'code': 'recognition_uncertain', 'fields': ['raw_value']},))),
])
def test_distinct_or_unreliable_results_are_never_folded(first, second):
    assert len(fold_cells((first, second))) == 2


def test_reference_differences_fold_without_a_unified_abnormal_conclusion():
    groups = fold_cells((cell('a', reference='1-10', flag='H'), cell('b', reference='2-9')))
    assert len(groups) == 1 and groups[0].reference_difference
    assert groups[0].abnormal.status == 'review'
    assert groups[0].abnormal.symbol == ''
    assert [(row.reference_range_raw, row.report_flag_raw) for row in groups[0].sources] == [('1-10', 'H'), ('2-9', '')]


def test_latest_daily_point_comes_from_real_sampling_time_not_input_order():
    points, disputed = latest_daily_cells((cell('b', '6', at='2026-09-17 10:30'), cell('a', '5')))
    assert [point.observation.pk for point in points] == ['b']
    assert not disputed


def test_latest_equal_group_uses_its_latest_source_time():
    groups = fold_cells((cell('a', '5'), cell('b', '6', at='2026-09-17 09:00'), cell('c', '5', at='2026-09-17 10:30')))
    points, disputed = latest_daily_cells(groups)
    assert len(points) == 1 and points[0].observation.raw_value == '5'
    assert points[0].latest_sampled_at == datetime(2026, 9, 17, 10, 30)
    assert not disputed


def test_latest_same_time_conflict_does_not_fall_back_to_earlier_point():
    points, disputed = latest_daily_cells((cell('a', '4'), cell('b', '5', at='2026-09-17 10:30'), cell('c', '6', at='2026-09-17 10:30')))
    assert points == ()
    assert {row.pk for point in disputed for row in point.sources} == {'a', 'b', 'c'}


def test_time_conflict_blocks_latest_instead_of_silently_skipping_dispute():
    conflict = cell('b', '6')
    conflict.observation.report_identity = replace(conflict.observation.report_identity, sampled_at=None,
        status='REVIEW', reason='sampling_datetime_conflict')
    points, disputed = latest_daily_cells((cell('a', '4'), conflict))
    assert points == () and len(disputed) == 2


def test_hospitals_and_incomparable_methods_have_independent_daily_points():
    points, disputed = latest_daily_cells((cell('a'), cell('b', institution='其他医院'), cell('c', method='method-b')))
    assert len(points) == 3 and not disputed


def test_unreliable_latest_point_does_not_promote_an_older_eligible_point():
    latest = replace(cell('b', at='2026-09-17 12:00'), trend_eligible=False, plot_eligible=False)
    points, disputed = latest_daily_cells((cell('a'), latest))
    assert len(points) == 1 and points[0].observation.pk == 'b'
    assert not points[0].trend_eligible


def test_quality_group_change_does_not_hide_the_latest_dispute():
    latest = cell('b', at='2026-09-17 12:00')
    latest = replace(latest, trend_eligible=False, plot_eligible=False,
                     group_key=latest.group_key[:4] + ('insufficient',))
    points, _ = latest_daily_cells((cell('a'), latest))
    assert [point.observation.pk for point in points] == ['b']


def test_folding_does_not_erase_incomparable_method_series():
    groups = fold_cells((cell('a'), cell('b', method='method-b', at='2026-09-17 10:30')))
    assert len(groups) == 1
    points, disputed = latest_daily_cells(groups)
    assert {point.group_key[3] for point in points} == {'method-a', 'method-b'}
    assert not disputed


def test_folding_does_not_promote_the_quality_of_a_weaker_source():
    weaker = replace(cell('b'), trend_eligible=False, plot_eligible=False, comparability='insufficient')
    for items in ((cell('a'), weaker), (weaker, cell('a'))):
        folded = fold_cells(items)[0]
        assert not folded.trend_eligible and not folded.plot_eligible
        assert folded.comparability == 'insufficient'


def test_scope_filtering_before_folding_hides_unselected_counts_and_latest_time():
    selected = [item for item in (cell('allowed'), cell('private', at='2026-09-17 12:00')) if item.observation.pk == 'allowed']
    points, _ = latest_daily_cells(fold_cells(selected))
    assert len(points[0].sources) == 1
    assert points[0].latest_sampled_at == datetime(2026, 9, 17, 8, 30)
