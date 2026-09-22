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
    ('采样时间：2026-02-3008:30', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-1724:30', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-1708:60', 'sampling_datetime_unreliable'),
    ('采样时间：2026-09-1708:30:99', 'sampling_datetime_unreliable'),
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
    ('采样时间：2026-09-1708:30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
    ('采样时间：2026-09-1708:30:05', datetime(2026, 9, 17, 8, 30, 5), 'SECOND'),
    ('采样时间：2026/09/1708:30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
    ('采样时间：2026.09.1708:30', datetime(2026, 9, 17, 8, 30), 'MINUTE'),
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


@pytest.mark.parametrize('label', ['血常规五分类', '本检验报告仅对本次送检标本负责', '请结合临床分析检验结果'])
def test_report_category_and_disclaimer_do_not_start_another_report(label):
    result = extract_report_units((page('合成医院检验报告单', '姓名：合成人甲', label,
        '采样时间：2026-09-17 08:30', '白细胞 5'),))
    assert len(result) == 1
    assert result[0].status == 'ACCEPTED'


def stitched_report_page(identifier='报告号：A1'):
    regions = []
    for index, offset in enumerate((.05, .5), 1):
        lines = [('第%d页 共2页' % index, .8, offset, .96, offset + .02),
                 ('合成医院检验报告单', .25, offset + .005, .75, offset + .04),
                 (identifier, .1, offset + .06, .5, offset + .08),
                 ('患者ID号：P100', .1, offset + .09, .5, offset + .11)]
        name, value, unit = ('白细胞', '5.0', '10^9/L') if index == 1 else ('红细胞', '4.2', '10^12/L')
        lines.extend([(name, .05, offset + .15, .3, offset + .17),
                      (value, .4, offset + .15, .5, offset + .17),
                      (unit, .6, offset + .15, .8, offset + .17)])
        if index == 1:
            lines.append(('采样时间：2026-09-1708:30', .1, offset + .25, .6, offset + .27))
        for text, left, top, right, bottom in lines:
            regions.append(OcrRegion(text, ((left, top), (right, top), (right, bottom), (left, bottom)), .99, len(regions)))
    return OcrPage(1, 1000, 2000, tuple(regions), 'synthetic', '1',
                   source_transform=((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)))


def test_stitched_page_counter_before_title_belongs_to_its_own_report():
    first, second = extract_report_units((stitched_report_page(),))
    assert [(item.page_index, item.page_total) for item in (first, second)] == [(1, 2), (2, 2)]
    assert first.status == 'ACCEPTED'
    output = resolve_continuation_times([('main', first), ('continuation', second)])
    assert output['continuation'].sampled_at == datetime(2026, 9, 17, 8, 30)
    assert output['continuation'].time_source == 'main'


@pytest.mark.parametrize('title', ['合成医院生化检验报告单', '检验报告单（生化）', '血常规检验报告单',
                                 '检验报告单 空腹血糖', '合成医院检验报告单 生化检查（空腹血糖）'])
def test_qualified_report_titles_keep_independent_report_times(title):
    result = extract_report_units((page(title, '报告号：A1', '采样时间：2026-09-17 08:30',
        '白细胞 5', title, '报告号：A2', '采样时间：2026-09-18 08:30', '红细胞 4'),))
    assert len(result) == 2
    assert [(item.report_number, item.status, item.sampled_at) for item in result] == [
        ('A1', 'ACCEPTED', datetime(2026, 9, 17, 8, 30)),
        ('A2', 'ACCEPTED', datetime(2026, 9, 18, 8, 30)),
    ]


def test_hospital_before_page_counter_stays_with_continuation_header():
    from dataclasses import replace
    original = stitched_report_page()
    regions = []
    for region in original.regions:
        if region.text.startswith('第'):
            top = min(point[1] for point in region.polygon)
            regions.append(OcrRegion('合成医院', ((.1, top - .025), (.5, top - .025),
                (.5, top - .005), (.1, top - .005)), .99, len(regions)))
        regions.append(replace(region, text=region.text.replace('合成医院检验报告单', '检验报告单'), reading_order=len(regions)))
    first, second = extract_report_units((replace(original, regions=tuple(regions)),))
    assert first.institution == second.institution == '合成医院'
    assert resolve_continuation_times([('main', first), ('continuation', second)])['continuation'].status == 'ACCEPTED'


def test_barcode_continuation_can_use_time_only_within_one_original_image():
    first, second = extract_report_units((stitched_report_page('条码号：B100'),))
    output = resolve_continuation_times([('image:1:1', first), ('image:1:2', second)])
    assert output['image:1:2'].status == 'ACCEPTED'
    assert output['image:1:2'].sampled_at == datetime(2026, 9, 17, 8, 30)
    assert output['image:1:2'].time_source == 'image:1:1'
    assert first.report_number == second.report_number == ''


@pytest.mark.parametrize('conflict', ['indicator_identity', 'reported_error', 'revision_conflict'])
def test_barcode_identity_does_not_override_confirmed_result_conflicts(conflict):
    from types import SimpleNamespace
    from apps.labs.dictionary import phase_two_dictionary
    from apps.labs.reports import _pair_basis
    from apps.labs.validation import indicator_identity_issue

    first, second = extract_report_units((stitched_report_page('条码号：B100'),))
    left, right = SimpleNamespace(source_key='image:1:1'), SimpleNamespace(source_key='image:1:2')
    resolved = resolve_continuation_times(((left.source_key, first), (right.source_key, second)))
    dictionary = phase_two_dictionary()
    rows = []
    for index, code in enumerate(('LAB_ALB', 'LAB_HGB')):
        row = SimpleNamespace(pk=str(index), standard_code=code,
            raw_name='白蛋白' if index == 0 or conflict == 'indicator_identity' else '血红蛋白',
            raw_value='5', raw_unit='g/L', specimen='BLOOD', result_type='NUMERIC',
            dictionary_version=dictionary.version, quality_issues=[], revision_number=0,
            evidence=SimpleNamespace(source_text='synthetic', polygon=None, confidence=.99), field_evidence={})
        row.indicator_identity_issue = indicator_identity_issue(row, dictionary)
        rows.append(row)
    if conflict == 'indicator_identity':
        assert rows[1].indicator_identity_issue['code'] == 'association_conflict'
    else:
        setattr(rows[1], conflict, True)
    _, _, reliable, basis, _ = _pair_basis(left, right, sources={
        left.source_key: (resolved[left.source_key], (rows[0],)),
        right.source_key: (resolved[right.source_key], (rows[1],)),
    })
    assert not reliable
    assert basis['identity_or_result_uncertain']


@pytest.mark.parametrize('change', ['other_image', 'other_page', 'barcode', 'patient', 'hospital',
                                  'missing_patient', 'missing_page', 'page_total', 'low_confidence', 'conflicting_result'])
def test_barcode_continuation_requires_all_original_identity_evidence(change):
    from dataclasses import replace
    original = stitched_report_page('条码号：B100')
    regions = list(original.regions)
    for index, region in enumerate(regions):
        if min(point[1] for point in region.polygon) < .5:
            continue
        text = region.text
        if change == 'barcode':
            text = text.replace('B100', 'B200')
        if change == 'patient':
            text = text.replace('P100', 'P200')
        if change == 'hospital':
            text = text.replace('合成医院', '另一医院')
        if change == 'missing_patient' and text.startswith('患者ID号'):
            text = '姓名：合成人甲'
        if change == 'missing_page' and text.startswith('第'):
            text = '续页'
        if change == 'page_total':
            text = text.replace('共2页', '共3页')
        regions[index] = replace(region, text=text, confidence=.5 if change == 'low_confidence' and text.startswith('条码号') else region.confidence)
    first, second = extract_report_units((replace(original, regions=tuple(regions)),))
    other_key = 'other:1:2' if change == 'other_image' else 'image:2:2' if change == 'other_page' else 'image:1:2'
    conflicts = {frozenset(('image:1:1', other_key))} if change == 'conflicting_result' else set()
    output = resolve_continuation_times([('image:1:1', first), (other_key, second)], conflicting_pairs=conflicts)
    assert output[other_key].status == 'REJECTED'
    assert output[other_key].sampled_at is None


@pytest.mark.parametrize('conflicting_text', ['患者ID号：P200', '患者ID号：P200\n患者ID号：P100', '报告号：R100\n报告号：R200'])
def test_barcode_cannot_override_conflicting_identity_on_a_continuation(conflicting_text):
    from dataclasses import replace
    original = stitched_report_page('条码号：B100')
    last = original.regions[-1]
    extra = replace(last, text=conflicting_text, reading_order=last.reading_order + 1)
    first, second = extract_report_units((replace(original, regions=(*original.regions, extra)),))
    output = resolve_continuation_times([('image:1:1', first), ('image:1:2', second)])
    assert output['image:1:2'].status == 'REJECTED'


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
