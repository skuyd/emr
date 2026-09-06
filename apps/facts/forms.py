from django import forms

from .models import FactCategory


class ManualFactForm(forms.Form):
    category = forms.ChoiceField(label="事实类型", choices=FactCategory.choices)
    page_number = forms.IntegerField(label="原件页码", min_value=1)
    text = forms.CharField(label="完整原文摘录", max_length=30000, widget=forms.Textarea(attrs={"rows": 5}))


class FactRevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    category = forms.ChoiceField(label="事实类型", choices=FactCategory.choices)
    text = forms.CharField(label="完整摘录（可更正转录）", max_length=30000, widget=forms.Textarea(attrs={"rows": 7}))
    date_raw = forms.CharField(label="治疗日期原文（时间不详时可留空）", required=False, max_length=512)
    record_date_raw = forms.CharField(label="报告记载日期原文（可留空）", required=False, max_length=512)
    institution = forms.CharField(label="医院或机构原文", required=False, max_length=512)
    checked_original = forms.BooleanField(label="我已对照原件核对完整摘录及限定表达", required=False)
