from uuid import uuid4

from django import forms
from django.views.decorators.debug import sensitive_variables


DECISIONS = [('CONFIRM', '确认原页来源'), ('CORRECT', '更正地址或标题'), ('REASSIGN', '调整报告归属'),
             ('RECHECK', '从当前原页重新补录'), ('EXCLUDE', '排除此来源'), ('UNDO', '撤销最近决定并重新核对')]


class OpenForm(forms.Form):
    expected_source = forms.RegexField(regex=r'\A[a-f0-9]{64}\Z', max_length=64, strip=False)
    expected_revision = forms.IntegerField(min_value=1)

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        allowed = {'csrfmiddlewaretoken', 'patient_id', 'expected_source', 'expected_revision'}
        if set(self.data) - allowed or any(len(self.data.getlist(key)) != 1 for key in self.data):
            raise forms.ValidationError('提交内容无效，请从当前来源说明页打开。')
        return data


class ScanForm(forms.Form):
    expected_source = forms.RegexField(regex=r'^[a-f0-9]{64}$', widget=forms.HiddenInput)
    operation_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, material, **kwargs):
        kwargs.setdefault('initial', {}).update(expected_source=material['input_token'], operation_id=uuid4())
        super().__init__(*args, **kwargs)


class ManualSourceForm(ScanForm):
    title = forms.CharField(label='来源标题', max_length=160, required=False)
    url = forms.CharField(label='原页上的完整地址', max_length=8192, strip=False,
                          widget=forms.Textarea(attrs={'rows': 3, 'autocomplete': 'off', 'spellcheck': 'false'}))
    page_id = forms.ChoiceField(label='原件页', choices=[])
    report_id = forms.ChoiceField(label='报告归属', required=False, choices=[])

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, material=material, **kwargs)
        self.fields['page_id'].choices = [(str(page['id']), f"第 {page['page_number']} 页") for page in material['pages']]
        self.fields['report_id'].choices = [('', '资料级来源，暂不归属报告')] + [(row['id'], row['title']) for row in material['reports']]


class DecisionForm(ManualSourceForm):
    action = forms.ChoiceField(label='核对操作', choices=DECISIONS)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label='我已对照当前原页核对来源', required=False)

    def __init__(self, *args, material, row, **kwargs):
        super().__init__(*args, material=material, **kwargs)
        self.fields['url'].required = self.fields['page_id'].required = False
        self.initial.update(expected_revision=row['revision_number'], expected_source=row['source_token'],
                            title=row['title'], url=row['url'], page_id=row['evidence']['page_id'], report_id=row['report_id'] or '')
