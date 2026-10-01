from uuid import uuid4

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
    record_date = forms.DateField(label='日期', widget=forms.DateInput(attrs={'type': 'date'}))
    value = forms.CharField(label='数值', max_length=64, strip=False, widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    unit = forms.ChoiceField(label='单位', choices=[])
    symptom_name = forms.CharField(label='症状名称', max_length=80, strip=False)
    severity = forms.CharField(label='自述程度（可留空）', max_length=80, strip=False, required=False)
    score = forms.ChoiceField(label='ECOG评分', choices=[('', '请选择等级'), *((str(n), f'{n} 分') for n in range(6))],
                              widget=forms.RadioSelect)

    def __init__(self, *args, record=None, kind='WEIGHT', **kwargs):
        self.record = record
        now = timezone.localtime()
        initial = {'kind': kind, 'creation_key': uuid4(),
                   'measured_local': now.strftime('%Y-%m-%dT%H:%M'), 'record_date': now.date()}
        if record is not None:
            data = record.current_data
            initial.update(kind=record.kind, measured_local=data['local_time'],
                           record_date=record.record_date, value=data.get('raw_value', ''),
                           unit=data.get('raw_unit', ''), symptom_name=data.get('symptom_name', ''),
                           severity=data.get('severity', ''), score=data.get('score'),
                           expected_revision=record.revision_number)
        initial.update(kwargs.pop('initial', {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        self.kind = record.kind if record is not None else (self.data.get('kind', initial['kind']) if self.is_bound else initial['kind'])
        self.kind_label = dict(DailyRecord.Kind.choices).get(self.kind, '记录')
        if self.kind == 'ECOG':
            for field in ('measured_local', 'value', 'unit', 'symptom_name', 'severity'):
                self.fields.pop(field)
        elif self.kind == 'SYMPTOM':
            for field in ('record_date', 'value', 'unit', 'score'):
                self.fields.pop(field)
            self.fields['measured_local'].label = '发生时间'
        else:
            for field in ('record_date', 'symptom_name', 'severity', 'score'):
                self.fields.pop(field)
            units = ['°C', '°F'] if self.kind == 'TEMPERATURE' else ['kg', 'g', 'lb']
            self.fields['unit'].choices = [(unit, unit) for unit in units]
            self.initial.setdefault('unit', units[0])
        if record is None:
            self.fields['creation_key'].required = True
        else:
            self.fields['expected_revision'].required = True

    def payload(self):
        data = {'kind': self.cleaned_data['kind']}
        if self.kind == 'ECOG':
            data.update(record_date=self.cleaned_data['record_date'].isoformat(), score=self.cleaned_data['score'])
        else:
            data['measured_local'] = self.cleaned_data['measured_local']
            if self.kind == 'SYMPTOM':
                data.update(symptom_name=self.cleaned_data['symptom_name'], severity=self.cleaned_data['severity'])
            else:
                data.update(value=self.cleaned_data['value'], unit=self.cleaned_data['unit'])
        return data

    def clean(self):
        cleaned = super().clean()
        if self.record is not None and cleaned.get('kind') != self.record.kind:
            self.add_error('kind', '更正时不能改变记录类型。')
        if not self.errors:
            try:
                normalize_payload(self.payload())
            except InvalidRecord as error:
                self.add_error(error.field if error.field in self.fields else None, str(error))
        return cleaned


class RevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class RecordChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, record):
        data = record.current_data
        value = (' · '.join(item for item in (data['symptom_name'], data['severity']) if item)
                 if record.kind == 'SYMPTOM' else f"{data['raw_value']} {data['raw_unit']}")
        return f"{record.get_kind_display()} · {data['local_time']} · {value}"
