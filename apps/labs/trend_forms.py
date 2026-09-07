from django import forms


class JointTrendForm(forms.Form):
    code = forms.MultipleChoiceField(label='选择指标', required=False,
                                     widget=forms.CheckboxSelectMultiple,
                                     help_text='每次最多选择 8 项，各自保留单位和可比依据。')
    start = forms.DateField(label='开始日期', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='结束日期', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, summaries=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].choices = [(item.standard_code, item.standard_name) for item in summaries]

    def clean_code(self):
        codes = tuple(dict.fromkeys(self.cleaned_data['code']))
        if len(codes) > 8:
            raise forms.ValidationError('请最多选择 8 项指标。')
        return codes

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start') and cleaned.get('end') and cleaned['start'] > cleaned['end']:
            raise forms.ValidationError('开始日期不能晚于结束日期。')
        return cleaned
