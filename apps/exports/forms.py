from django import forms

from apps.facts.readmodels import review_facts
from apps.facts.clinical_readmodels import report_material
from apps.labs.readmodels import effective_rows
from apps.self_records.forms import RecordChoices
from apps.self_records.models import DailyRecord
from apps.glucose.forms import RecordChoices as GlucoseChoices
from apps.glucose.models import GlucoseRecord

from .content import SECTIONS
from .formats import FORMAT_CHOICES, PART_CHOICES
from .selection import select_documents


class SelectionForm(forms.Form):
    self_record_ids = RecordChoices(label='日常记录（仅纳入明确勾选的记录）', required=False,
                                   queryset=DailyRecord.objects.none(), widget=forms.CheckboxSelectMultiple)
    glucose_record_ids = GlucoseChoices(label='血糖记录（仅纳入明确勾选的记录）', required=False,
                                       queryset=GlucoseRecord.objects.none(), widget=forms.CheckboxSelectMultiple)
    mode = forms.ChoiceField(label="资料范围", choices=(("all", "全部正常资料"), ("documents", "按资料勾选"), ("dates", "按资料日期筛选")))
    document_ids = forms.MultipleChoiceField(label="按资料选择", required=False, widget=forms.CheckboxSelectMultiple)
    start = forms.DateField(label="开始日期（含）", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="结束日期（含）", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    unknown_ids = forms.MultipleChoiceField(label="日期不确定资料：明确选择纳入", required=False, widget=forms.CheckboxSelectMultiple)
    nickname = forms.CharField(label="姓名或昵称", max_length=80)
    basic_info = forms.CharField(label="基本信息（可留空）", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sections = forms.MultipleChoiceField(label="速查卡内容", choices=SECTIONS, required=False, widget=forms.CheckboxSelectMultiple)
    custom_facts = forms.BooleanField(label="自选已核对事实（报告或字段细选时默认不附带旧摘录）", required=False)
    fact_ids = forms.MultipleChoiceField(label="已核对事实", required=False, widget=forms.CheckboxSelectMultiple)
    custom_reports = forms.BooleanField(label="自选结构化报告", required=False)
    report_ids = forms.MultipleChoiceField(label="报告范围", required=False, widget=forms.CheckboxSelectMultiple)
    custom_clinical_fields = forms.BooleanField(label="自选已核对的报告字段", required=False)
    clinical_field_ids = forms.MultipleChoiceField(label="已核对字段", required=False, widget=forms.CheckboxSelectMultiple)
    custom_observations = forms.BooleanField(label="自选导出的检验结果（报告或字段细选时须明确选择）", required=False)
    observation_ids = forms.MultipleChoiceField(label="导出检验结果", required=False, widget=forms.CheckboxSelectMultiple)
    custom_labs = forms.BooleanField(label="自选重点检验指标（不勾选时展示全部可用指标的最近结果）", required=False)
    lab_codes = forms.MultipleChoiceField(label="重点检验指标", required=False, widget=forms.CheckboxSelectMultiple)
    details = forms.BooleanField(label="允许附页：正文超出 A4 一页时将完整明细放入附页", required=False)

    def __init__(self, patient, *args, actor=None, **kwargs):
        initial = {"mode": "all", "nickname": patient.display_name, "sections": [key for key, _ in SECTIONS]}
        initial.update(kwargs.pop("initial", {}) or {})
        if "fact_ids" in initial:
            initial["custom_facts"] = True
        if "lab_codes" in initial:
            initial["custom_labs"] = True
        if "report_ids" in initial:
            initial["custom_reports"] = True
        if "clinical_field_ids" in initial:
            initial["custom_clinical_fields"] = True
        if "observation_ids" in initial:
            initial["custom_observations"] = True
        super().__init__(*args, initial=initial, **kwargs)
        self.fields['self_record_ids'].queryset = DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True)
        self.fields['glucose_record_ids'].queryset = GlucoseRecord.objects.filter(patient=patient, deleted_at__isnull=True)
        self.documents = select_documents(patient, {"mode": "all"})["documents"]
        choices = [(row["id"], f'{row["filename"]} · {row["date_raw"] or "日期未明确"}') for row in self.documents]
        self.fields["document_ids"].choices = choices
        self.fields["unknown_ids"].choices = choices
        self.fields["fact_ids"].choices = [
            (row["id"], f'{row["category_label"]}：{row["content"]["text"]}（{row["source"]["filename"]} 第 {row["source"]["page"]} 页）')
            for row in review_facts(patient) if row["usable"]
        ]
        observations = effective_rows(patient, include_uncertain=True)
        codes = {row.standard_code: row.standard_name or row.raw_name for row in observations}
        self.fields["lab_codes"].choices = sorted(codes.items())
        self.fields["observation_ids"].choices = [
            (str(row.pk), f'{row.standard_name or row.raw_name}：{row.raw_value} {row.raw_unit} · {row.observation_date or "日期不详"}')
            for row in observations]
        reports = [row for row in report_material(patient) if row["source_valid"] and row["status"] == "ACTIVE"]
        self.fields["report_ids"].choices = [(row["id"], f'{row["title"]} · 第 {", ".join(map(str, row["pages"]))} 页') for row in reports]
        self.fields["clinical_field_ids"].choices = [
            (field["id"], f'{row["title"]} · {field["field_label"]}：{field["content"]["text"]}')
            for row in reports for field in row["fields"] if field["usable"]
        ]
        from .treatment_forms import add_derived_fields
        add_derived_fields(self, patient, actor=actor)

    def selection(self):
        result = {key: value for key, value in self.cleaned_data.items() if key not in {"custom_facts", "custom_labs", "custom_reports", "custom_clinical_fields", "custom_observations"}}
        result['self_record_ids'] = [str(row.pk) for row in self.cleaned_data['self_record_ids']]
        result['glucose_record_ids'] = [str(row.pk) for row in self.cleaned_data['glucose_record_ids']]
        for key in ("start", "end"):
            result[key] = result[key].isoformat() if result[key] else ""
        if not self.cleaned_data["custom_facts"]:
            result.pop("fact_ids")
        if not self.cleaned_data["custom_labs"]:
            result.pop("lab_codes")
        if not self.cleaned_data["custom_reports"]:
            result.pop("report_ids")
        if not self.cleaned_data["custom_clinical_fields"]:
            result.pop("clinical_field_ids")
        if not self.cleaned_data["custom_observations"]:
            result.pop("observation_ids")
        if result["mode"] != "dates":
            result["unknown_ids"] = []
        if result["mode"] != "documents":
            result["document_ids"] = []
        from .treatment_forms import derived_selection
        result.update(derived_selection(self.cleaned_data))
        return result


class GenerationForm(forms.Form):
    format = forms.ChoiceField(label="导出格式", choices=FORMAT_CHOICES)
    parts = forms.MultipleChoiceField(label="资料包内容（仅 ZIP）", choices=PART_CHOICES, required=False, widget=forms.CheckboxSelectMultiple)
