from django import forms

from apps.facts.readmodels import review_facts
from apps.labs.readmodels import effective_rows

from .content import SECTIONS
from .formats import FORMAT_CHOICES, PART_CHOICES
from .selection import select_documents


class SelectionForm(forms.Form):
    mode = forms.ChoiceField(label="资料范围", choices=(("all", "全部正常资料"), ("documents", "按资料勾选"), ("dates", "按资料日期筛选")))
    document_ids = forms.MultipleChoiceField(label="按资料选择", required=False, widget=forms.CheckboxSelectMultiple)
    start = forms.DateField(label="开始日期（含）", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="结束日期（含）", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    unknown_ids = forms.MultipleChoiceField(label="日期不确定资料：明确选择纳入", required=False, widget=forms.CheckboxSelectMultiple)
    nickname = forms.CharField(label="姓名或昵称", max_length=80)
    basic_info = forms.CharField(label="基本信息（可留空）", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sections = forms.MultipleChoiceField(label="速查卡内容", choices=SECTIONS, required=False, widget=forms.CheckboxSelectMultiple)
    custom_facts = forms.BooleanField(label="自选已核对事实（不勾选时纳入所选资料的全部有效事实）", required=False)
    fact_ids = forms.MultipleChoiceField(label="已核对事实", required=False, widget=forms.CheckboxSelectMultiple)
    custom_labs = forms.BooleanField(label="自选重点检验指标（不勾选时展示全部可用指标的最近结果）", required=False)
    lab_codes = forms.MultipleChoiceField(label="重点检验指标", required=False, widget=forms.CheckboxSelectMultiple)
    details = forms.BooleanField(label="允许附页：正文超出 A4 一页时将完整明细放入附页", required=False)

    def __init__(self, patient, *args, **kwargs):
        initial = {"mode": "all", "nickname": patient.display_name, "sections": [key for key, _ in SECTIONS]}
        initial.update(kwargs.pop("initial", {}) or {})
        if "fact_ids" in initial:
            initial["custom_facts"] = True
        if "lab_codes" in initial:
            initial["custom_labs"] = True
        super().__init__(*args, initial=initial, **kwargs)
        self.documents = select_documents(patient, {"mode": "all"})["documents"]
        choices = [(row["id"], f'{row["filename"]} · {row["date_raw"] or "日期未明确"}') for row in self.documents]
        self.fields["document_ids"].choices = choices
        self.fields["unknown_ids"].choices = choices
        self.fields["fact_ids"].choices = [
            (row["id"], f'{row["category_label"]}：{row["content"]["text"]}（{row["source"]["filename"]} 第 {row["source"]["page"]} 页）')
            for row in review_facts(patient) if row["usable"]
        ]
        codes = {row.standard_code: row.standard_name or row.raw_name for row in effective_rows(patient, include_uncertain=True)}
        self.fields["lab_codes"].choices = sorted(codes.items())

    def selection(self):
        result = {key: value for key, value in self.cleaned_data.items() if key not in {"custom_facts", "custom_labs"}}
        for key in ("start", "end"):
            result[key] = result[key].isoformat() if result[key] else ""
        if not self.cleaned_data["custom_facts"]:
            result.pop("fact_ids")
        if not self.cleaned_data["custom_labs"]:
            result.pop("lab_codes")
        if result["mode"] != "dates":
            result["unknown_ids"] = []
        if result["mode"] != "documents":
            result["document_ids"] = []
        return result


class GenerationForm(forms.Form):
    format = forms.ChoiceField(label="导出格式", choices=FORMAT_CHOICES)
    parts = forms.MultipleChoiceField(label="资料包内容（仅 ZIP）", choices=PART_CHOICES, required=False, widget=forms.CheckboxSelectMultiple)
