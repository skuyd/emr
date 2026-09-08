from decimal import Decimal, localcontext

import pytest

from apps.glucose.payloads import GlucoseInputError, TIME_SLOTS, normalize_payload


def payload(**changes):
    return {'value': '5.50', 'unit': 'mmol/L', 'time_precision': 'MINUTE',
            'measured_local': '2026-09-08T08:25', 'timezone': 'Asia/Shanghai',
            'timezone_origin': 'USER_CONFIRMED',
            'time_slot': 'UNSPECIFIED', 'source_label': '', 'notes': '', **changes}


@pytest.mark.parametrize('unit,value,expected', [('mmol/L', '5.50', '5.50'), ('mg/dL', '100', '5.551'),
                                                ('mg/dL', '-2', '-0.11102'), ('mg/dL', '0', '0')])
def test_original_quantity_and_sourced_unit_conversion_stay_separate(unit, value, expected):
    result = normalize_payload(payload(value=value, unit=unit))
    assert result['raw_value'] == value and result['raw_unit'] == unit
    assert result['result_type'] == 'NUMERIC' and result['plot_eligible']
    assert Decimal(result['normalized_value']) == Decimal(expected)
    assert result['normalized_unit'] == 'mmol/L'
    assert result['conversion']['rule_id']
    if unit == 'mg/dL':
        assert result['conversion']['factor'] == '0.05551'
        assert result['conversion']['source_url'] == 'https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/GLU_L.htm'


def test_conversion_has_its_own_decimal_context_and_keeps_unit_spelling():
    with localcontext() as context:
        context.prec = 2
        result = normalize_payload(payload(value='00100.000', unit=' mg ／ dL '))
    assert result['raw_value'] == '00100.000' and result['raw_unit'] == ' mg ／ dL '
    assert Decimal(result['normalized_value']) == Decimal('5.551')


@pytest.mark.parametrize('value,result_type', [('>30', 'COMPARATOR'), ('≤2.0', 'COMPARATOR'),
                                            ('4.0-8.0', 'RANGE'), ('5～9', 'RANGE'), ('HI', 'STATUS'), ('LO', 'STATUS')])
def test_non_single_values_keep_raw_text_and_never_become_a_curve_point(value, result_type):
    result = normalize_payload(payload(value=value), source_kind='METER')
    assert result['raw_value'] == value and result['result_type'] == result_type
    assert result['normalized_value'] is None and not result['plot_eligible']
    assert result['unplottable_reason']


def test_source_unknown_units_or_text_are_retained_without_a_fake_standard_value():
    result = normalize_payload(payload(value='未报告', unit='原文未注明'), source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['raw_value'] == '未报告' and result['raw_unit'] == '原文未注明'
    assert result['normalized_value'] is None and result['conversion'] is None
    assert not result['plot_eligible']
    with pytest.raises(GlucoseInputError):
        normalize_payload(payload(unit='原文未注明'))


@pytest.mark.parametrize('slot', ['FASTING', 'AFTER_BREAKFAST_2H', 'BEFORE_LUNCH', 'AFTER_LUNCH_2H',
                                 'BEFORE_DINNER', 'AFTER_DINNER_2H', 'BEDTIME', 'AT_0300', 'RANDOM', 'UNSPECIFIED'])
def test_slots_are_explicit_and_do_not_follow_the_clock(slot):
    assert slot in TIME_SLOTS
    result = normalize_payload(payload(time_slot=slot, measured_local='2026-09-08T12:00'))
    assert result['time_slot'] == slot
    assert normalize_payload(payload(measured_local='2026-09-08T03:00'))['time_slot'] == 'UNSPECIFIED'


def test_minute_time_retains_local_time_zone_offset_and_actual_instant():
    result = normalize_payload(payload())
    assert result['measured_at'] == '2026-09-08T00:25:00+00:00'
    assert result['local_time'] == '2026-09-08T08:25'
    assert result['measured_local_raw'] == '2026-09-08T08:25'
    assert result['measured_date'] == '2026-09-08'
    assert result['time_precision'] == 'MINUTE' and result['utc_offset'] == '+08:00'


def test_report_seconds_are_not_silently_rounded_to_the_minute():
    result = normalize_payload(payload(measured_local='2026-09-08T08:25:47', time_precision='SECOND'),
                               source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['measured_at'] == '2026-09-08T00:25:47+00:00'
    assert result['local_time'] == result['measured_local_raw'] == '2026-09-08T08:25:47'
    assert result['time_precision'] == 'SECOND' and result['plot_eligible']


@pytest.mark.parametrize('precision,raw,day', [('DAY', '2026-09-08', '2026-09-08'), ('MONTH', '2026-09', None),
                                            ('YEAR', '2026', None), ('UNKNOWN', '原件时间未注明', None)])
def test_imprecise_source_time_does_not_invent_a_datetime(precision, raw, day):
    data = payload(time_precision=precision, measured_local=raw)
    result = normalize_payload(data, source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['time_precision'] == precision and result['measured_local_raw'] == raw
    assert result['measured_at'] is None and result['measured_date'] == day
    assert not result['plot_eligible'] and result['unplottable_reason']
    with pytest.raises(GlucoseInputError):
        normalize_payload(data)


@pytest.mark.parametrize('raw', ['2026-03-29T02:30', '2026-10-25T02:30'])
def test_nonexistent_or_ambiguous_minutes_are_not_chosen(raw):
    with pytest.raises(GlucoseInputError):
        normalize_payload(payload(measured_local=raw, timezone='Europe/Berlin'))


def test_each_explicit_fold_is_a_distinct_instant():
    first = normalize_payload(payload(measured_local='2026-10-25T02:30+02:00', timezone='Europe/Berlin'))
    second = normalize_payload(payload(measured_local='2026-10-25T02:30+01:00', timezone='Europe/Berlin'))
    assert first['measured_at'] == '2026-10-25T00:30:00+00:00'
    assert second['measured_at'] == '2026-10-25T01:30:00+00:00'
    assert first['local_time'] == second['local_time'] and first['utc_offset'] != second['utc_offset']


@pytest.mark.parametrize('changes', [{'value': True}, {'value': 'NaN'}, {'value': '1e9999'}, {'value': 'abc'},
                                   {'time_slot': 'INFER_FROM_CLOCK'}, {'timezone': 'bad/zone'},
                                   {'measured_local': '2026-09-08'}, {'measured_local': '2026-09-08T08:25:47'},
                                   {'measured_local': '2026-09-08T08:25+01:00'}, {'notes': 'x' * 501}])
def test_invalid_input_is_rejected_without_clinical_clipping(changes):
    with pytest.raises(GlucoseInputError):
        normalize_payload(payload(**changes))


def test_timezone_date_boundary_is_preserved_instead_of_deduplicated_by_utc_date():
    result = normalize_payload(payload(measured_local='2026-09-08T00:01'))
    assert result['measured_at'] == '2026-09-07T16:01:00+00:00'
    assert result['measured_date'] == '2026-09-08'


@pytest.mark.parametrize('timezone_name', ['', 'Asia/Shanghai'])
def test_report_wall_seconds_without_confirmed_zone_do_not_claim_a_utc_instant(timezone_name):
    result = normalize_payload(payload(measured_local='2026-09-08T08:25:47', time_precision='SECOND',
                                       timezone=timezone_name, timezone_origin='UNCONFIRMED'),
                               source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['measured_at'] is None and result['utc_offset'] == ''
    assert result['time_precision'] == 'SECOND' and result['measured_date'] == '2026-09-08'
    assert result['measured_local_raw'] == result['local_time'] == '2026-09-08T08:25:47'
    assert result['timezone'] == '' and result['timezone_origin'] == 'UNCONFIRMED'
    assert not result['plot_eligible'] and '时区' in result['unplottable_reason']


def test_report_timezone_without_origin_is_unconfirmed_even_when_a_default_zone_is_present():
    data = payload()
    del data['timezone_origin']
    result = normalize_payload(data, source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['timezone_origin'] == 'UNCONFIRMED' and result['measured_at'] is None


@pytest.mark.parametrize('origin', ['USER_CONFIRMED', 'SOURCE_EXPLICIT'])
def test_confirmed_report_zone_retains_its_origin(origin):
    result = normalize_payload(payload(timezone_origin=origin), source_kind='LAB_REPORT', allow_imprecise=True)
    assert result['timezone_origin'] == origin and result['plot_eligible']
    assert result['measured_at'] == '2026-09-08T00:25:00+00:00'


def test_unconfirmed_manual_time_is_rejected():
    with pytest.raises(GlucoseInputError):
        normalize_payload(payload(timezone_origin='UNCONFIRMED'))


@pytest.mark.parametrize('precision,raw', [('DAY', '2026-02-30'), ('MONTH', '2026-13'),
                                        ('YEAR', '0000'), ('SECOND', '2026-02-30T08:25:47')])
def test_source_time_without_zone_still_validates_the_original_calendar(precision, raw):
    with pytest.raises(GlucoseInputError):
        normalize_payload(payload(time_precision=precision, measured_local=raw, timezone='',
                                  timezone_origin='UNCONFIRMED'), source_kind='LAB_REPORT', allow_imprecise=True)
