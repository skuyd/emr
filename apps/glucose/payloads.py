"""Glucose input with explicit precision, provenance and decimal conversion."""

from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE_KINDS = ('LAB_REPORT', 'NURSING', 'METER', 'MANUAL')
TIME_SLOTS = {
    'FASTING': '空腹', 'AFTER_BREAKFAST_2H': '早餐后 2 小时', 'BEFORE_LUNCH': '午餐前',
    'AFTER_LUNCH_2H': '午餐后 2 小时', 'BEFORE_DINNER': '晚餐前',
    'AFTER_DINNER_2H': '晚餐后 2 小时', 'BEDTIME': '睡前', 'AT_0300': '凌晨 3 点',
    'RANDOM': '随机', 'UNSPECIFIED': '未注明',
}
TIME_PRECISIONS = ('SECOND', 'MINUTE', 'DAY', 'MONTH', 'YEAR', 'UNKNOWN')
NUMBER_TEXT = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
NUMBER = re.compile(NUMBER_TEXT + r'\Z')
COMPARATOR = re.compile(r'(?:<=|>=|<|>|≤|≥)\s*' + NUMBER_TEXT + r'\Z')
RANGE = re.compile(NUMBER_TEXT + r'\s*(?:-|~|至)\s*' + NUMBER_TEXT + r'\Z')
MINUTE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::00)?(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z')
SECOND = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z')
CALCULATION = Context(prec=50, Emax=100, Emin=-100)
CONVERSION_SOURCE = 'https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/GLU_L.htm'


class GlucoseInputError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def _text(data, name, maximum, *, required=False):
    value = data.get(name, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise GlucoseInputError(name, f'请填写不超过 {maximum} 个字符的文字。')
    if required and not value.strip():
        raise GlucoseInputError(name, '请填写这一项。')
    return value


def _time(data, *, allow_imprecise, source_kind):
    precision = _text(data, 'time_precision', 10)
    if precision not in TIME_PRECISIONS:
        raise GlucoseInputError('time_precision', '请选择原始时间精度。')
    raw = _text(data, 'measured_local', 160, required=precision != 'UNKNOWN')
    default_origin = 'UNCONFIRMED' if source_kind in ('LAB_REPORT', 'NURSING') else 'USER_CONFIRMED'
    origin = _text({'timezone_origin': data.get('timezone_origin', default_origin)}, 'timezone_origin', 20)
    if origin not in ('UNCONFIRMED', 'USER_CONFIRMED', 'SOURCE_EXPLICIT'):
        raise GlucoseInputError('timezone_origin', '请核对时区依据。')
    unconfirmed = origin == 'UNCONFIRMED'
    if unconfirmed and (not allow_imprecise or source_kind not in ('LAB_REPORT', 'NURSING')):
        raise GlucoseInputError('timezone', '逐次测量需要明确确认时区。')
    name = _text(data, 'timezone', 80, required=not unconfirmed).strip()
    zone = None
    if unconfirmed:
        name = ''
    else:
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise GlucoseInputError('timezone', '请选择有效时区。') from None
    result = {'measured_local_raw': raw, 'time_precision': precision, 'timezone': name, 'timezone_origin': origin,
              'measured_at': None, 'measured_date': None, 'local_time': raw, 'utc_offset': ''}
    if precision not in ('MINUTE', 'SECOND'):
        if not allow_imprecise:
            raise GlucoseInputError('measured_local', '逐次测量需要完整日期和至少分钟精度。')
        try:
            if precision == 'DAY':
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', raw):
                    raise ValueError
                result['measured_date'] = date.fromisoformat(raw).isoformat()
            elif precision == 'MONTH':
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', raw):
                    raise ValueError
                date.fromisoformat(raw + '-01')
            elif precision == 'YEAR':
                if not re.fullmatch(r'[0-9]{4}', raw) or not 1 <= int(raw) <= 9999:
                    raise ValueError
        except ValueError:
            raise GlucoseInputError('measured_local', '日期与所选精度不符，请核对。') from None
        return result
    pattern = MINUTE if precision == 'MINUTE' else SECOND
    if not pattern.fullmatch(raw):
        raise GlucoseInputError('measured_local', '日期和时间与所选精度不符，不会自动补充或舍去秒值。')
    try:
        entered = datetime.fromisoformat(raw)
    except ValueError:
        raise GlucoseInputError('measured_local', '日期或时间无效，请核对。') from None
    wall = entered.replace(tzinfo=None)
    if unconfirmed:
        return {**result, 'measured_date': wall.date().isoformat(),
                'local_time': wall.isoformat(timespec='minutes' if precision == 'MINUTE' else 'seconds')}
    candidates = {}
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        try:
            instant = candidate.astimezone(timezone.utc)
            round_trip = instant.astimezone(zone)
        except (OverflowError, ValueError):
            continue
        if round_trip.replace(tzinfo=None) == wall:
            candidates[instant] = candidate
    if entered.tzinfo is not None:
        candidates = {instant: candidate for instant, candidate in candidates.items()
                      if candidate.utcoffset() == entered.utcoffset()}
    if not candidates:
        raise GlucoseInputError('measured_local', '该当地时间不存在，或偏移与所选时区不符。')
    if len(candidates) > 1:
        raise GlucoseInputError('measured_local', '该当地时间重复出现，请用明确的时区偏移区分。')
    instant, candidate = next(iter(candidates.items()))
    offset = candidate.strftime('%z')
    offset = offset[:3] + ':' + offset[3:5] + (':' + offset[5:] if len(offset) > 5 else '')
    return {**result, 'measured_at': instant.isoformat(), 'measured_date': wall.date().isoformat(),
            'local_time': wall.isoformat(timespec='minutes' if precision == 'MINUTE' else 'seconds'),
            'utc_offset': offset}


def _quantity(data, *, source_kind):
    raw = _text(data, 'value', 160, required=True)
    raw_unit = _text(data, 'unit', 80)
    value_text = unicodedata.normalize('NFKC', raw).strip().replace('−', '-').replace('–', '-').replace('—', '-')
    unit_text = ''.join(unicodedata.normalize('NFKC', raw_unit).split()).lower()
    unit = {'mmol/l': 'mmol/L', 'mg/dl': 'mg/dL'}.get(unit_text)
    source_text_allowed = source_kind in ('LAB_REPORT', 'NURSING')
    if unit is None and not source_text_allowed:
        raise GlucoseInputError('unit', '请选择 mmol/L 或 mg/dL。')
    if NUMBER.fullmatch(value_text):
        result_type = 'NUMERIC'
    elif COMPARATOR.fullmatch(value_text):
        result_type = 'COMPARATOR'
    elif RANGE.fullmatch(value_text):
        result_type = 'RANGE'
    elif value_text.upper() in ('HI', 'LO'):
        result_type = 'STATUS'
    elif source_text_allowed:
        result_type = 'TEXT'
    else:
        raise GlucoseInputError('value', '请填写有限数值、比较符、范围或血糖仪 HI/LO 状态。')
    result = {'raw_value': raw, 'raw_unit': raw_unit, 'result_type': result_type,
              'normalized_value': None, 'normalized_unit': '', 'conversion': None,
              'unplottable_reason': ''}
    if result_type != 'NUMERIC':
        result['unplottable_reason'] = '原文不是明确单值，保留原文且不生成曲线点。'
        return result
    if unit is None:
        result['unplottable_reason'] = '原始单位未能确认，未生成标准值或曲线点。'
        return result
    try:
        value = Decimal(value_text)
    except InvalidOperation:
        raise GlucoseInputError('value', '数值格式无效，请核对。') from None
    if (not value.is_finite() or len(value_text) > 64 or len(value.as_tuple().digits) > 40
            or abs(value.adjusted()) > 30):
        if not source_text_allowed:
            raise GlucoseInputError('value', '数值超出系统支持的表示范围，请核对原值。')
        result['unplottable_reason'] = '原始数值超出系统支持的表示范围，保留原文。'
        return result
    factor = '1' if unit == 'mmol/L' else '0.05551'
    with localcontext(CALCULATION):
        converted = value * Decimal(factor)
        normalized = str(converted.normalize()) if converted else '0'
    result.update(normalized_value=normalized, normalized_unit='mmol/L', conversion={
        'rule_id': 'glucose-mmol-l-identity-v1' if unit == 'mmol/L' else 'glucose-mg-dl-mmol-l-cdc-v1',
        'factor': factor, 'formula': 'value' if unit == 'mmol/L' else 'value * 0.05551',
        'source_url': '' if unit == 'mmol/L' else CONVERSION_SOURCE,
    })
    return result


def normalize_payload(data, *, source_kind='MANUAL', allow_imprecise=False):
    if not isinstance(data, dict):
        raise GlucoseInputError('__all__', '记录内容无效。')
    if source_kind not in SOURCE_KINDS:
        raise GlucoseInputError('source_kind', '请选择有效来源。')
    slot = _text(data, 'time_slot', 24)
    if slot not in TIME_SLOTS:
        raise GlucoseInputError('time_slot', '请选择明确时段；未注明时不会按时钟推断。')
    result = {'schema_version': '1.0', 'source_kind': source_kind, 'time_slot': slot,
              **_time(data, allow_imprecise=allow_imprecise, source_kind=source_kind), **_quantity(data, source_kind=source_kind),
              'source_label': _text(data, 'source_label', 80), 'notes': _text(data, 'notes', 500)}
    if result['measured_at'] is None:
        reason = ('原始时区未确认，保留当地时间但不生成 UTC 时刻或日内曲线点。'
                  if result['time_precision'] in ('MINUTE', 'SECOND') else '原始时间未明确到分钟，不生成日内曲线点。')
        result['unplottable_reason'] = ' '.join(filter(None, [result['unplottable_reason'], reason]))
    result['plot_eligible'] = result['normalized_value'] is not None and result['measured_at'] is not None
    return result
