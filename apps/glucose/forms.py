from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.utils import timezone

from .history import SOURCE_LABELS
from .payloads import GlucoseInputError, TIME_SLOTS, normalize_payload


PRECISIONS = [('SECOND', '精确到秒'), ('MINUTE', '精确到分钟'), ('DAY', '仅日期'),
              ('MONTH', '仅年月'), ('YEAR', '仅年份'), ('UNKNOWN', '时间不详')]
OFFSET_HELP = '夏令时结束时，同一时刻可能出现两次，例如 -04:00 或 -05:00。更正时，日期、时间、精度和时区均未变则保留原时刻。'


def _offset_field():
    return forms.RegexField(label='重复时刻的 UTC 偏移（可留空）', regex=r'^[+-][0-9]{2}:[0-9]{2}$',
                            max_length=6, required=False, help_text=OFFSET_HELP)


def _source_fingerprint_field():
    return forms.RegexField(regex=r'^[a-f0-9]{64}$', widget=forms.HiddenInput)


class GlucoseRecordForm(forms.Form):
    source_kind = forms.ChoiceField(label='记录来源', choices=[])
    creation_key = forms.UUIDField(required=False, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    value = forms.CharField(label='原始血糖结果', max_length=160, strip=False,
                            help_text='保留原值、范围、比较符或 HI/LO；范围不会取中点。')
    unit = forms.CharField(label='原始单位', max_length=80, strip=False, required=False,
                           widget=forms.TextInput(attrs={'list': 'glucose-units'}))
    measured_local = forms.CharField(label='测量时间', max_length=160, strip=False, required=False)
    time_precision = forms.ChoiceField(label='原始时间精度', choices=PRECISIONS)
    timezone = forms.CharField(label='测量所在时区', max_length=80, required=False,
                               widget=forms.TextInput(attrs={'list': 'glucose-timezones'}))
    confirm_timezone = forms.BooleanField(label='我确认记录所在时区', required=False,
        help_text='未确认时保留原件当地时间，暂不生成日内曲线点。')
    utc_offset = _offset_field()
    time_slot = forms.ChoiceField(label='原文或本人确认的时段', choices=list(TIME_SLOTS.items()),
        help_text='未注明时段会单独保留；不会根据钟点推断空腹或餐后。')
    source_label = forms.CharField(label='设备或测量方式（可留空）', max_length=80, strip=False, required=False)
    notes = forms.CharField(label='备注（可留空）', max_length=500, strip=False, required=False,
                            widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, record=None, source_kind='MANUAL', **kwargs):
        self.record = record
        self.source_kind = record.source_kind if record is not None else source_kind
        self.from_original = self.source_kind in ('LAB_REPORT', 'NURSING')
        initial = {'source_kind': self.source_kind, 'creation_key': uuid4(), 'unit': 'mmol/L',
                   'time_slot': 'UNSPECIFIED', 'time_precision': 'UNKNOWN' if self.from_original else 'MINUTE',
                   'measured_local': '' if self.from_original else timezone.now().astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='minutes')[:16],
                   'timezone': '' if self.from_original else 'Asia/Shanghai'}
        if record is not None:
            data = record.current_data
            initial.update({key: data.get(key, '') for key in ('time_precision', 'timezone', 'time_slot', 'notes', 'source_label')})
            initial.update(value=data['raw_value'], unit=data['raw_unit'], measured_local=data['local_time'],
                           creation_key=record.creation_key, expected_revision=record.revision_number,
                           confirm_timezone=data['timezone_origin'] != 'UNCONFIRMED')
        initial.update(kwargs.pop('initial', {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        choices = [(self.source_kind, SOURCE_LABELS[self.source_kind])] if record is not None or self.from_original else [
            (key, SOURCE_LABELS[key]) for key in ('MANUAL', 'METER')]
        self.fields['source_kind'].choices = choices
        if record is not None or self.from_original:
            self.fields['source_kind'].widget = forms.HiddenInput()
        if self.from_original:
            self.fields['measured_local'].help_text = '按原件精度填写，如 2026-08-02T06:12:34、2026-08-02、2026-08 或 2026；不详可留空。'
        else:
            self.fields.pop('confirm_timezone')
            self.fields['time_precision'].choices = PRECISIONS[:2]
            self.fields['measured_local'].required = self.fields['timezone'].required = True
            self.fields['measured_local'].widget = forms.TextInput(attrs={'type': 'datetime-local', 'step': '1'})
            self.fields['unit'] = forms.ChoiceField(label='原始单位', choices=[('mmol/L', 'mmol/L'), ('mg/dL', 'mg/dL')])
        self.fields['creation_key' if record is None else 'expected_revision'].required = True

    def payload(self):
        data = {key: self.cleaned_data.get(key, '') for key in (
            'value', 'unit', 'measured_local', 'time_precision', 'timezone', 'time_slot', 'source_label', 'notes')}
        confirmed = not self.from_original or self.cleaned_data.get('confirm_timezone', False)
        data['timezone_origin'] = 'USER_CONFIRMED' if confirmed else 'UNCONFIRMED'
        if (confirmed and self.record is not None and self.record.current_data['timezone_origin'] == 'SOURCE_EXPLICIT'
                and data['timezone'] == self.record.current_data['timezone']):
            data['timezone_origin'] = 'SOURCE_EXPLICIT'
        if self.cleaned_data.get('utc_offset'):
            data['measured_local'] += self.cleaned_data['utc_offset']
        elif (self.record is not None and data['measured_local'] == self.record.current_data['local_time']
              and data['timezone'] == self.record.current_data['timezone']
              and data['time_precision'] == self.record.current_data['time_precision']):
            data['measured_local'] = self.record.current_data['measured_local_raw']
        return data

    def clean(self):
        cleaned = super().clean()
        if not self.errors:
            try:
                normalize_payload(self.payload(), source_kind=cleaned['source_kind'], allow_imprecise=self.from_original)
            except GlucoseInputError as error:
                self.add_error(error.field if error.field in self.fields else None, str(error))
        return cleaned


class NursingImportForm(GlucoseRecordForm):
    expected_source = _source_fingerprint_field()
    original_excerpt = forms.CharField(label='原件对应文字', max_length=2000, strip=False,
                                       widget=forms.Textarea(attrs={'rows': 4}))
    measurement_scope = forms.ChoiceField(label='原文记录方式', choices=[('SINGLE', '一次明确测量'), ('SUMMARY', '一段时间的汇总')],
        help_text='汇总保留原文，不拆成逐次测量。医嘱、剂量和未来监测计划不能录入为测量。')
    checked_original = forms.BooleanField(label='已对照护理原件核对本次内容')

    def __init__(self, *args, candidate, record=None, **kwargs):
        initial = {'expected_source': candidate['source_fingerprint'], 'measurement_scope': 'SINGLE'}
        if record is not None:
            initial.update({key: record.current_data['source'].get(key, '') for key in ('original_excerpt', 'measurement_scope')})
        initial.update(kwargs.pop('initial', {}) or {})
        super().__init__(*args, source_kind='NURSING', record=record, initial=initial, **kwargs)
        self.initial['creation_key'] = uuid4()
        self.fields['creation_key'].required = True


class LabImportForm(forms.Form):
    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_source = _source_fingerprint_field()
    expected_revision = forms.IntegerField(min_value=0, required=False, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label='已对照原件核对项目、标本、原值、单位和采样时间')
    confirm_timezone = forms.BooleanField(label='我确认记录所在时区', required=False,
        help_text='留空会保留原件当地时间和原有精度；不会按医院位置自动补时区。')
    timezone = forms.CharField(label='采样所在时区', max_length=80, required=False,
                               widget=forms.TextInput(attrs={'list': 'glucose-timezones'}))
    utc_offset = _offset_field()

    def __init__(self, *args, candidate, record=None, **kwargs):
        initial = {'creation_key': uuid4(), 'expected_source': candidate['source_fingerprint']}
        if record is not None:
            initial['expected_revision'] = record.revision_number
        initial.update(kwargs.pop('initial', {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        self.fields['expected_revision'].required = record is not None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('confirm_timezone'):
            try:
                ZoneInfo(cleaned.get('timezone', ''))
            except (ZoneInfoNotFoundError, ValueError):
                self.add_error('timezone', '请选择有效时区。')
        elif cleaned.get('utc_offset'):
            self.add_error('utc_offset', '请先明确确认时区，再填写重复时刻的偏移。')
        return cleaned


class RevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class HistoryFilterForm(forms.Form):
    source_kind = forms.ChoiceField(label='记录来源', choices=[('', '全部来源'), *SOURCE_LABELS.items()], required=False)
    source_label = forms.CharField(label='设备或测量方式', max_length=80, required=False)
    time_slot = forms.ChoiceField(label='时段', choices=[('', '全部时段'), *TIME_SLOTS.items()], required=False)
    date_scope = forms.ChoiceField(label='日期范围', choices=[('ALL', '全部记录'), ('DATED', '有完整日期'), ('UNKNOWN', '日期不详')], required=False)
    display_timezone = forms.CharField(label='显示时区（可选）', max_length=80, required=False,
        widget=forms.TextInput(attrs={'placeholder': '例如 Asia/Shanghai、UTC'}),
        help_text='留空按原记录当地时间。选择时区后，只换算已有确定时刻的记录。')
    start = forms.DateField(label='开始日期（含）', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='结束日期（含）', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean_display_timezone(self):
        name = self.cleaned_data['display_timezone'].strip()
        if name:
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                raise forms.ValidationError('请选择有效的 IANA 时区，例如 Asia/Shanghai 或 UTC。') from None
        return name

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start') and cleaned.get('end') and cleaned['start'] > cleaned['end']:
            self.add_error('end', '结束日期不能早于开始日期。')
        return cleaned
