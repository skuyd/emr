from decimal import Decimal

import pytest

from apps.self_records.payloads import InvalidRecord, normalize_payload


def payload(**changes):
    return {
        'kind': 'WEIGHT', 'measured_local': '2026-09-08T08:25', 'timezone': 'Asia/Shanghai',
        'value': '60.0', 'unit': 'kg', 'notes': '', 'source_label': '',
        **changes,
    }


@pytest.mark.parametrize('unit,value,expected', [('kg', '60.0', '60'), ('g', '1250', '1.25'), ('lb', '2', '0.90718474')])
def test_weight_keeps_original_input_and_explicit_conversion(unit, value, expected):
    result = normalize_payload(payload(unit=unit, value=value))
    assert result['raw_value'] == value and result['raw_unit'] == unit
    assert result['normalized_unit'] == 'kg'
    assert Decimal(result['normalized_value']) == Decimal(expected)
    assert result['conversion']['rule_id'] and result['conversion']['formula']
    assert result['measured_at'] == '2026-09-08T00:25:00+00:00'
    assert result['local_time'] == '2026-09-08T08:25'
    assert result['timezone'] == 'Asia/Shanghai' and result['utc_offset'] == '+08:00'


def test_temperature_does_not_use_the_callers_decimal_context():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 3
        result = normalize_payload(payload(kind='TEMPERATURE', unit='°F', value='98.6'))
    assert Decimal(result['normalized_value']) == Decimal('37')
    assert result['raw_value'] == '98.6' and result['raw_unit'] == '°F'
    assert result['normalized_unit'] == '°C'


def test_finite_values_are_not_clipped_to_a_medical_reference_range():
    result = normalize_payload(payload(value='12345.6789'))
    assert Decimal(result['normalized_value']) == Decimal('12345.6789')
    assert result['raw_value'] == '12345.6789'


def test_symptom_retains_the_patients_words_without_assigning_a_numeric_risk():
    result = normalize_payload(payload(kind='SYMPTOM', value='', unit='', symptom_name='乏力', severity='比昨天明显', notes='步行后出现'))
    assert result['symptom_name'] == '乏力' and result['severity'] == '比昨天明显'
    assert result['notes'] == '步行后出现'
    assert result['normalized_value'] is None and result['normalized_unit'] == ''


@pytest.mark.parametrize('changes', [
    {'value': 'NaN'}, {'value': 'Infinity'}, {'value': True}, {'value': '>60'}, {'value': '1e100000'},
    {'kind': 'GLUCOSE'}, {'unit': '°C'}, {'kind': 'TEMPERATURE', 'unit': 'kg'},
    {'kind': 'SYMPTOM', 'value': '', 'unit': '', 'symptom_name': ''},
    {'notes': 'a' * 501}, {'measured_local': '2026-09-08'}, {'timezone': 'unknown/place'},
    {'measured_local': '2026-09-08T08:25:30'},
])
def test_invalid_format_or_wrong_record_type_is_rejected(changes):
    with pytest.raises(InvalidRecord):
        normalize_payload(payload(**changes))


@pytest.mark.parametrize('local_time', ['2026-03-29T02:30', '2026-10-25T02:30'])
def test_nonexistent_or_ambiguous_local_minute_is_not_guessed(local_time):
    with pytest.raises(InvalidRecord):
        normalize_payload(payload(timezone='Europe/Berlin', measured_local=local_time))


def test_explicit_offset_can_disambiguate_a_repeated_minute():
    first = normalize_payload(payload(timezone='Europe/Berlin', measured_local='2026-10-25T02:30+02:00'))
    second = normalize_payload(payload(timezone='Europe/Berlin', measured_local='2026-10-25T02:30+01:00'))
    assert first['measured_at'] == '2026-10-25T00:30:00+00:00'
    assert second['measured_at'] == '2026-10-25T01:30:00+00:00'
    assert first['utc_offset'] != second['utc_offset']


def test_offset_must_describe_the_chosen_local_timezone():
    with pytest.raises(InvalidRecord):
        normalize_payload(payload(measured_local='2026-09-08T08:25+01:00'))
