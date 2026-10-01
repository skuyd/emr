from decimal import Decimal

import pytest

from apps.self_records.payloads import InvalidRecord, normalize_payload


def payload(**changes):
    return {
        'kind': 'WEIGHT', 'measured_local': '2026-09-08T08:25',
        'value': '60.0', 'unit': 'kg', **changes,
    }


@pytest.mark.parametrize('unit,value,expected', [('kg', '60.0', '60'), ('g', '1250', '1.25'), ('lb', '2', '0.90718474')])
def test_weight_keeps_original_input_and_explicit_conversion(unit, value, expected):
    result = normalize_payload(payload(unit=unit, value=value))
    assert result['raw_value'] == value and result['raw_unit'] == unit
    assert result['normalized_unit'] == 'kg'
    assert Decimal(result['normalized_value']) == Decimal(expected)
    assert result['conversion']['rule_id'] and result['conversion']['formula']
    assert result['local_time'] == '2026-09-08T08:25'
    assert result['record_date'] == '2026-09-08' and result['record_time'] == '08:25'
    assert 'measured_at' not in result and 'timezone' not in result


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
    result = normalize_payload(payload(kind='SYMPTOM', symptom_name=' 乏力 ', severity=' 比昨天明显 '))
    assert result['symptom_name'] == '乏力' and result['severity'] == '比昨天明显'
    assert result['normalized_value'] is None and result['normalized_unit'] == ''


@pytest.mark.parametrize('changes', [
    {'value': 'NaN'}, {'value': 'Infinity'}, {'value': True}, {'value': '>60'}, {'value': '1e100000'},
    {'kind': 'GLUCOSE'}, {'unit': '°C'}, {'kind': 'TEMPERATURE', 'unit': 'kg'},
    {'kind': 'SYMPTOM', 'symptom_name': ''},
    {'measured_local': '2026-09-08'}, {'measured_local': '2026-09-08T08:25:30'},
    {'kind': 'ECOG', 'record_date': '2026-09-08', 'score': ''},
])
def test_invalid_format_or_wrong_record_type_is_rejected(changes):
    with pytest.raises(InvalidRecord):
        normalize_payload(payload(**changes))


@pytest.mark.parametrize('local_time', ['2026-03-29T02:30', '2026-10-25T02:30'])
def test_dst_local_minute_needs_no_offset_or_timezone(local_time):
    result = normalize_payload(payload(measured_local=local_time))
    assert result['local_time'] == local_time


def test_offset_bearing_input_is_rejected_as_not_a_local_minute():
    with pytest.raises(InvalidRecord) as error:
        normalize_payload(payload(measured_local='2026-10-25T02:30+01:00'))
    assert error.value.field == 'measured_local'
