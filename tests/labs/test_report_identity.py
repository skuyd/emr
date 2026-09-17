"""Synthetic original-page evidence; no real patient material is used."""

from datetime import datetime

import pytest

from apps.processing.value_objects import OcrPage, OcrRegion
from apps.labs.report_identity import extract_report_units, resolve_continuation_times


def page(*texts, number=1, confidence=.99):
    return OcrPage(number, 1000, 1000, tuple(
        OcrRegion(text, ((.1, i / 30), (.9, i / 30), (.9, (i + .5) / 30), (.1, (i + .5) / 30)), confidence, i)
        for i, text in enumerate(texts)), 'synthetic', '1')


def unit(*texts, **kwargs):
    return extract_report_units((page('合成医院', '检验报告', '报告号：R100', *texts, **kwargs),))[0]


@pytest.mark.parametrize('text,reason', [
    ('采样日期：2026-09-17', 'sampling_time_missing'),
    ('采样时间：08:30', 'sampling_date_missing'),
    ('报告时间：2026-09-17 08:30', 'sampling_datetime_missing'),
    ('打印时间：2026-09-17 08:30', 'sampling_datetime_missing'),
    ('检查日期：2026-09-17 08:30', 'sampling_datetime_missing'),
    ('采样时间：2026-02-30 08:30', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-17 24:30', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-17 08:60', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-17 08:30:99', 'sampling_datetime_unreliable'),
])
def test_incomplete_or_invalid_sampling_is_not_admitted(text, reason):
    result = unit(text)
    assert result.status == 'REJECTED'
    assert result.reason == reason
    assert result.sampled_at is None


@pytest.mark.parametrize('text,expected,precision', [
    ('采样时间：2026-09-17 08:30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
    ('采样时间：2026/9/17 8:30:05', datetime(2026, 9, 17, 8, 30, 5), 'SECOND'),
    ('采集时间：2026年9月17日 08：30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
    ('采血时间：2026.09.17 08:30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
])
def test_time_keeps_original_precision_and_location(text, expected, precision):
    result = unit(text)
    assert (result.status, result.sampled_at, result.precision) == ('ACCEPTED', expected, precision)
    evidence = result.fields['sampled_at'][0]
    assert evidence['raw_text'] == text
    assert evidence['page_number'] == 1
    assert evidence['reading_order'] == 3
    assert evidence['polygon']


def test_report_time_on_same_line_cannot_supply_missing_sampling_clock():
    result = unit('采样日期：2026-09-17 报告时间：2026-09-17 08:30')
    assert result.status == 'REJECTED'
    assert result.reason == 'sampling_time_missing'


def test_explicit_separate_sampling_date_and_clock_can_be_combined():
    result = unit('采样日期：2026-09-17', '采样时间：08:30')
    assert result.sampled_at == datetime(2026, 9, 17, 8, 30)
    assert len(result.fields['sampled_at']) == 2


def test_conflicting_complete_times_are_retained_for_review():
    result = unit('采样时间：2026-09-17 08:30', '采样时间：2026-09-17 09:30')
    assert (result.status, result.reason, result.sampled_at) == ('REVIEW', 'sampling_datetime_conflict', None)
    assert len(result.fields['sampled_at']) == 2


def test_separate_date_candidates_with_clock_are_conflict_not_missing():
    result = unit('采样日期：2026-09-17', '采样日期：2026-09-18', '采样时间：08:30')
    assert (result.status, result.reason) == ('REVIEW', 'sampling_datetime_conflict')


def test_compatible_minute_and_second_evidence_preserves_seconds():
    result = unit('采样时间：2026-09-17 08:30', '采样时间：2026-09-17 08:30:25')
    assert (result.status, result.sampled_at, result.precision) == ('ACCEPTED', datetime(2026, 9, 17, 8, 30, 25), 'SECOND')


def test_low_confidence_complete_time_is_reviewed_without_claiming_reliable_identity():
    result = unit('采样时间：2026-09-17 08:30', confidence=.6)
    assert result.status == 'REVIEW'
    assert result.reason == 'sampling_datetime_unreliable'
    assert not result.identity_reliable


def test_non_lab_material_is_not_subject_to_sampling_admission():
    for label in ('病理报告', '影像报告', '处方'):
        assert extract_report_units((page('合成医院', label),)) == ()


def test_two_reports_on_one_page_keep_independent_evidence_ranges():
    result = extract_report_units((page('合成医院', '检验报告', '报告号：A1',
        '采样时间：2026-09-17 08:30', '白细胞 5', '合成医院', '检验报告', '报告号：B2',
        '采样时间：2026-09-17 09:30', '白细胞 6'),))
    assert len(result) == 2
    assert [item.report_number for item in result] == ['A1', 'B2']
    assert [item.sampled_at.hour for item in result] == [8, 9]
    assert result[0].end_order < result[1].start_order


def tabled_page(*headers):
    regions = []
    for index, texts in enumerate(headers):
        for text in texts:
            order = len(regions)
            top = order / 40
            regions.append(OcrRegion(text, ((.05, top), (.95, top), (.95, top + .015), (.05, top + .015)), .99, order))
        top = len(regions) / 40
        for left, right, text in ((.05, .3, '白细胞'), (.4, .5, str(5 + index)), (.6, .8, '10^9/L')):
            regions.append(OcrRegion(text, ((left, top), (right, top), (right, top + .015), (left, top + .015)), .99, len(regions)))
    return OcrPage(1, 1000, 1000, tuple(regions), 'synthetic', '1')


@pytest.mark.parametrize('second_time', ['2026-09-17 09:30', '2026-09-17', ''])
def test_titleless_report_headers_with_separate_tables_are_independent(second_time):
    from apps.labs.dictionary import default_dictionary
    from apps.labs.report_identity import recognize_report_units

    second = ['乙医院', '报告号：B2']
    if second_time:
        second.append('采样时间：' + second_time)
    original = tabled_page(('甲医院', '报告号：A1', '采样时间：2026-09-17 08:30'), second)
    result = recognize_report_units((original,), default_dictionary())
    assert len(result) == 2
    assert [item.report_number for item in result] == ['A1', 'B2']
    assert [item.institution for item in result] == ['甲医院', '乙医院']
    assert [item.status for item in result] == ['ACCEPTED', 'ACCEPTED' if ':' in second_time else 'REJECTED']
    assert result[0].end_order < result[1].start_order
    assert not result[1].time_source


def test_sampling_headers_without_report_numbers_split_only_across_tables():
    original = tabled_page(('甲医院', '采样时间：2026-09-17 08:30'),
                          ('采样时间：2026-09-17 09:30',))
    result = extract_report_units((original,), lab_page_numbers=(1,))
    assert len(result) == 2
    assert [item.sampling_label for item in result] == ['2026-09-17 08:30', '2026-09-17 09:30']
    assert result[1].institution == ''


def test_conflicting_headers_before_one_table_are_not_split_into_accepted_reports():
    original = tabled_page(('甲医院', '报告号：A1', '报告号：B2',
                           '采样时间：2026-09-17 08:30', '采样时间：2026-09-17 09:30'))
    result = extract_report_units((original,), lab_page_numbers=(1,))
    assert len(result) == 1
    assert result[0].status == 'REVIEW'
    assert result[0].sampled_at is None


def test_other_page_hospital_is_not_borrowed_without_report_link():
    result = extract_report_units((page('甲医院', '检验报告', '报告号：A1', '采样时间：2026-09-17 08:30'),
        page('检验报告', '报告号：B2', '采样时间：2026-09-17 09:30', number=2)))
    assert result[1].institution == ''
    assert not result[1].identity_reliable


def test_conflicting_hospital_or_patient_identity_never_allows_auto_merge():
    for texts in (('乙医院',), ('姓名：合成人甲', '姓名：合成人乙')):
        result = unit('采样时间：2026-09-17 08:30', *texts)
        assert result.status == 'REVIEW'
        assert not result.identity_reliable


@pytest.mark.parametrize('reverse', [False, True])
def test_continuation_with_explicit_page_identity_is_order_independent(reverse):
    main = unit('采样时间：2026-09-17 08:30', '第1页 共2页')
    continuation = unit('第2页 共2页', number=2)
    inputs = [('main', main), ('continuation', continuation)]
    if reverse:
        inputs.reverse()
    output = resolve_continuation_times(inputs)
    assert output['continuation'].status == 'ACCEPTED'
    assert output['continuation'].sampled_at == datetime(2026, 9, 17, 8, 30)
    assert output['continuation'].time_source == 'main'
    assert output['main'].time_source == ''


@pytest.mark.parametrize('extra', [(), ('第2页 共3页',), ('采样日期：2026-09-18', '第2页 共2页')])
def test_batch_membership_or_conflicting_date_is_not_continuation_proof(extra):
    output = resolve_continuation_times([('main', unit('采样时间：2026-09-17 08:30', '第1页 共2页')),
                                         ('other', unit(*extra, number=2))])
    assert output['other'].status == 'REJECTED'


def test_continuation_does_not_choose_between_conflicting_main_times():
    output = resolve_continuation_times([
        ('a', unit('采样时间：2026-09-17 08:30', '第1页 共2页')),
        ('b', unit('采样时间：2026-09-17 09:30', '第1页 共2页')),
        ('c', unit('第2页 共2页')),
    ])
    assert output['c'].status == 'REJECTED'


def test_continuation_requires_same_patient_and_nonconflicting_overlap():
    first = unit('采样时间：2026-09-17 08:30', '第1页 共2页', '姓名：合成人甲')
    second = unit('第2页 共2页', '姓名：合成人乙')
    assert resolve_continuation_times([('a', first), ('b', second)])['b'].status == 'REJECTED'
    second = unit('第2页 共2页', '姓名：合成人甲')
    assert resolve_continuation_times([('a', first), ('b', second)], conflicting_pairs={frozenset(('a', 'b'))})['b'].status == 'REJECTED'
