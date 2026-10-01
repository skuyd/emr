"""Explicit user-entered records; raw input and conversion stay separate."""

from datetime import date, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext
import re


KINDS = ('WEIGHT', 'TEMPERATURE', 'SYMPTOM', 'ECOG')
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
MINUTE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}\Z')
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
CALCULATION = Context(prec=50, Emax=100, Emin=-100)


class InvalidRecord(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def _text(data, name, maximum, *, required=False):
    value = data.get(name, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidRecord(name, f'请填写不超过 {maximum} 个字符的文字。')
    if required and not value.strip():
        raise InvalidRecord(name, '请填写这一项。')
    return value


def _measurement_time(data):
    raw = _text(data, 'measured_local', 40, required=True)
    if not MINUTE.fullmatch(raw):
        raise InvalidRecord('measured_local', '请填写完整日期和分钟，不会自动补充缺失时间。')
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidRecord('measured_local', '日期或时间无效，请核对。') from None
    return {
        'local_time': raw, 'record_date': raw[:10], 'record_time': raw[11:], 'time_precision': 'MINUTE',
    }


def _date_only(data):
    raw = _text(data, 'record_date', 10, required=True)
    if not DATE.fullmatch(raw):
        raise InvalidRecord('record_date', '请填写有效日期。')
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise InvalidRecord('record_date', '日期无效，请核对。') from None
    return {'local_time': raw, 'record_date': raw, 'record_time': None, 'time_precision': 'DATE'}


def _quantity(data, kind):
    raw = _text(data, 'value', 64, required=True)
    unit = _text(data, 'unit', 12, required=True)
    if not NUMBER.fullmatch(raw.strip()):
        raise InvalidRecord('value', '请填写明确的有限数值。')
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise InvalidRecord('value', '数值格式无效，请核对。') from None
    if not value.is_finite() or len(value.as_tuple().digits) > 40 or abs(value.adjusted()) > 30:
        raise InvalidRecord('value', '数值超出系统支持的表示范围，请核对原值。')
    with localcontext(CALCULATION):
        if kind == 'WEIGHT' and unit in {'kg', 'g', 'lb'}:
            factor = {'kg': '1', 'g': '0.001', 'lb': '0.45359237'}[unit]
            converted, normalized_unit = value * Decimal(factor), 'kg'
            rule = {'rule_id': f'weight-{unit}-kg-v1', 'formula': 'value * ' + factor}
        elif kind == 'TEMPERATURE' and unit in {'°C', '°F'}:
            converted = value if unit == '°C' else (value - Decimal('32')) * Decimal('5') / Decimal('9')
            normalized_unit = '°C'
            rule = {'rule_id': 'temperature-celsius-v1' if unit == '°C' else 'temperature-fahrenheit-celsius-v1',
                    'formula': 'value' if unit == '°C' else '(value - 32) * 5 / 9'}
        else:
            raise InvalidRecord('unit', '请选择与记录类型对应的单位。')
        normalized = str(converted.normalize()) if converted else '0'
    return {'raw_value': raw, 'raw_unit': unit, 'normalized_value': normalized,
            'normalized_unit': normalized_unit, 'conversion': rule}


def normalize_payload(data):
    if not isinstance(data, dict):
        raise InvalidRecord('__all__', '记录内容无效。')
    kind = data.get('kind')
    if kind not in KINDS:
        raise InvalidRecord('kind', '请选择体重、体温、症状或 ECOG 评分。')
    result = {'schema_version': '2.0', 'kind': kind,
              **(_date_only(data) if kind == 'ECOG' else _measurement_time(data)),
              'source_kind': 'SELF_RECORD', 'symptom_name': '', 'severity': ''}
    if kind == 'SYMPTOM':
        result.update(symptom_name=_text(data, 'symptom_name', 80, required=True).strip(),
                      severity=_text(data, 'severity', 80).strip(), raw_value='', raw_unit='',
                      normalized_value=None, normalized_unit='', conversion=None)
    elif kind == 'ECOG':
        score = data.get('score')
        if type(score) is int and 0 <= score <= 5:
            pass
        elif isinstance(score, str) and score in {'0', '1', '2', '3', '4', '5'}:
            score = int(score)
        else:
            raise InvalidRecord('score', '请选择 0 至 5 分的 ECOG 等级。')
        result.update(score=score, raw_value=str(score), raw_unit='分', normalized_value=None,
                      normalized_unit='', conversion=None)
    else:
        result.update(_quantity(data, kind))
    return result
