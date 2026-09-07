from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.utils import timezone

from .models import DailyRecord
from .payloads import InvalidRecord, normalize_payload


class RecordForm(forms.Form):
    kind = forms.ChoiceField(label='记录类型', choices=DailyRecord.Kind.choices, widget=forms.HiddenInput)
    creation_key = forms.UUIDField(required=False, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    measured_local = forms.CharField(label='测量或发生时间', max_length=40, strip=False,
                                    widget=forms.TextInput(attrs={'type': 'datetime-local', 'step': '60'}))
    timezone = forms.CharField(label='所在时区', max_length=80, widget=forms.TextInput(attrs={'list': 'record-timezones'}))
    utc_offset = forms.RegexField(label='重复时刻的 UTC 偏移（可留空）', regex=r'^[+-][0-9]{2}:[0-9]{2}$',
                                  required=False, max_length=6, help_text='夏令时结束时，同一时间可能出现两次；例如 +02:00 或 +01:00。更正时若日期、时间和时区未变，留空会保留原来的时刻。')
    value = forms.CharField(label='数值', max_length=64, strip=False, widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    unit = forms.ChoiceField(label='单位', choices=[])
    symptom_name = forms.CharField(label='症状名称', max_length=80, strip=False)
    severity = forms.CharField(label='自述程度（可留空）', max_length=80, strip=False, required=False)
    source_label = forms.CharField(label='测量方式或设备（可留空）', max_length=80, strip=False, required=False)
    notes = forms.CharField(label='备注（可留空）', max_length=500, strip=False, required=False,
                            widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, record=None, kind='WEIGHT', **kwargs):
        self.record = record
        initial = {'kind': kind, 'creation_key': uuid4(), 'timezone': 'Asia/Shanghai',
                   'measured_local': timezone.now().astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='minutes')[:16]}
        if record is not None:
            data = record.current_data
            initial.update({key: data.get(key, '') for key in ('kind', 'timezone', 'notes', 'source_label', 'symptom_name', 'severity')})
            initial.update(measured_local=data['local_time'], value=data['raw_value'], unit=data['raw_unit'],
                           creation_key=record.creation_key, expected_revision=record.revision_number)
        initial.update(kwargs.pop('initial', {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        self.kind = self.data.get('kind', initial['kind']) if self.is_bound else initial['kind']
        self.kind_label = dict(DailyRecord.Kind.choices).get(self.kind, '记录')
        if self.kind == 'SYMPTOM':
            self.fields.pop('value')
            self.fields.pop('unit')
        else:
            self.fields.pop('symptom_name')
            self.fields.pop('severity')
            units = ['°C', '°F'] if self.kind == 'TEMPERATURE' else ['kg', 'g', 'lb']
            self.fields['unit'].choices = [(unit, unit) for unit in units]
            self.initial.setdefault('unit', units[0])
        if record is None:
            self.fields['creation_key'].required = True
        else:
            self.fields['expected_revision'].required = True

    def payload(self):
        data = {key: self.cleaned_data.get(key, '') for key in (
            'kind', 'measured_local', 'timezone', 'value', 'unit', 'symptom_name', 'severity', 'source_label', 'notes',
        )}
        if self.cleaned_data.get('utc_offset'):
            data['measured_local'] += self.cleaned_data['utc_offset']
        elif (self.record is not None and data['measured_local'] == self.record.current_data['local_time']
              and data['timezone'] == self.record.current_data['timezone']):
            # Retain an existing explicit fold only for the same wall minute
            # and zone. A changed minute must be resolved from the new input.
            data['measured_local'] = self.record.current_data['measured_local_raw']
        return data

    def clean(self):
        cleaned = super().clean()
        if not self.errors:
            try:
                normalize_payload(self.payload())
            except InvalidRecord as error:
                self.add_error(error.field if error.field in self.fields else None, str(error))
        return cleaned


class HistoryFilterForm(forms.Form):
    kind = forms.ChoiceField(label='记录类型', choices=[('', '全部类型'), *DailyRecord.Kind.choices], required=False)
    start = forms.DateField(label='开始日期（含）', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='结束日期（含）', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    timezone = forms.CharField(label='显示和筛选时区', max_length=80, required=False,
                               widget=forms.TextInput(attrs={'list': 'record-timezones'}))

    def clean_timezone(self):
        name = self.cleaned_data['timezone'] or 'Asia/Shanghai'
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise forms.ValidationError('请选择有效时区。') from None
        return name

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start') and cleaned.get('end') and cleaned['start'] > cleaned['end']:
            self.add_error('end', '结束日期不能早于开始日期。')
        return cleaned


class RevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class RecordChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, record):
        data = record.current_data
        value = ' · '.join(item for item in (data['symptom_name'], data['severity']) if item) if record.kind == 'SYMPTOM' else f"{data['raw_value']} {data['raw_unit']}"
        return f"{record.get_kind_display()} · {data['local_time']} {data['timezone']} · {value}"
