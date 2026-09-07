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

    def __init__(self, patient, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["document_ids"].queryset = Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("-created_at", "pk")
        self.fields['self_record_ids'].queryset = DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True)
