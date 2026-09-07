from django import forms

from apps.documents.models import Document
from apps.exports.content import SECTIONS
from apps.self_records.forms import RecordChoices
from apps.self_records.models import DailyRecord


class DocumentChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, document):
        return document.display_filename


class ShareForm(forms.Form):
    document_ids = DocumentChoices(label="选择资料", queryset=Document.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    self_record_ids = RecordChoices(label='选择日常记录', queryset=DailyRecord.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    sections = forms.MultipleChoiceField(label="展示范围", choices=[
        (key, "原件来源（完整选定文件）" if key == "sources" else title) for key, title in SECTIONS
    ], widget=forms.CheckboxSelectMultiple, initial=["patient", "diagnosis", "treatment", "labs", "imaging", "self_records"])
    expires_in_hours = forms.IntegerField(label="有效期（小时）", initial=24, min_value=1, max_value=168, required=False)
    allow_original_download = forms.BooleanField(label="允许下载完整原件", required=False,
        help_text="撤销只能停止后续访问，已下载或自行保存的副本无法收回。")
    report_ids = forms.MultipleChoiceField(label="只分享这些已核对报告（可选）", required=False,
        choices=[], widget=forms.CheckboxSelectMultiple,
        help_text="留空时按资料和展示范围分享；选择后不附带其他报告、旧摘录、检验或完整原件。")
    clinical_field_ids = forms.MultipleChoiceField(label="只分享这些已核对字段（可选）", required=False,
        choices=[], widget=forms.CheckboxSelectMultiple,
        help_text="进一步限定字段，只展示选定值；完整核对上下文留在你的资料中。")

    def __init__(self, patient, *args, actor, **kwargs):
        from apps.facts.clinical_readmodels import review_reports

        super().__init__(*args, **kwargs)
        self.fields["document_ids"].queryset = Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("-created_at", "pk")
        self.fields['self_record_ids'].queryset = DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True)
        filenames = {str(row.pk): row.display_filename for row in self.fields["document_ids"].queryset}
        reports = [row for row in review_reports(patient, actor=actor) if any(field["usable"] for field in row["fields"])]
        self.fields["report_ids"].choices = [(row["id"], f"{filenames[row['document_id']]} · {row['title']}") for row in reports]
        self.fields["clinical_field_ids"].choices = [
            (field["id"], f"{filenames[row['document_id']]} · {field['field_label']}：{field['content']['text']}")
            for row in reports for field in row["fields"] if field["usable"]
        ]

    def selection(self):
        data = self.cleaned_data
        selection = {"document_ids": [str(row.pk) for row in data["document_ids"]], "sections": data["sections"],
                     "self_record_ids": [str(row.pk) for row in data['self_record_ids']]}
        for key in ("report_ids", "clinical_field_ids"):
            if data[key]:
                selection[key] = data[key]
        return selection
