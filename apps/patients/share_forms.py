from django import forms

from apps.documents.models import Document
from apps.exports.content import SECTIONS
from apps.self_records.forms import RecordChoices
from apps.self_records.models import DailyRecord
from apps.glucose.forms import RecordChoices as GlucoseChoices
from apps.glucose.models import GlucoseRecord


class DocumentChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, document):
        return document.display_filename


class ShareForm(forms.Form):
    custom_reports = forms.BooleanField(label='启用自选报告范围（空选不分享）', required=False)
    custom_clinical_fields = forms.BooleanField(label='启用自选字段范围（空选不分享）', required=False)
    document_ids = DocumentChoices(label="选择资料", queryset=Document.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    self_record_ids = RecordChoices(label='选择日常记录', queryset=DailyRecord.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    glucose_record_ids = GlucoseChoices(label='选择血糖记录', queryset=GlucoseRecord.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    sections = forms.MultipleChoiceField(label="展示范围", required=False, choices=[
        (key, "原件来源（完整选定文件）" if key == "sources" else title) for key, title in SECTIONS
    ], widget=forms.CheckboxSelectMultiple, initial=["patient", "diagnosis", "treatment", "labs", "imaging", "self_records", "glucose"])
    expires_in_hours = forms.IntegerField(label="有效期（小时）", initial=24, min_value=1, max_value=168, required=False)
    allow_original_download = forms.BooleanField(label="允许下载完整原件", required=False,
        help_text="撤销只能停止后续访问，已下载或自行保存的副本无法收回。")
    report_ids = forms.MultipleChoiceField(label="只分享这些已核对报告（可选）", required=False,
        choices=[], widget=forms.CheckboxSelectMultiple,
        help_text="留空时按资料和展示范围分享；选择后不附带其他报告、旧摘录、检验或完整原件。")
    clinical_field_ids = forms.MultipleChoiceField(label="只分享这些已核对字段（可选）", required=False,
        choices=[], widget=forms.CheckboxSelectMultiple,
        help_text="进一步限定字段。病理/IHC 的标记、评分和标本/检测归属会随所选结果保留；未选方法、抗体、原句和完整核对上下文留在你的资料中。")

    def __init__(self, patient, *args, actor, cancer_state=None, **kwargs):
        from apps.facts.clinical_readmodels import review_reports

        super().__init__(*args, **kwargs)
        from apps.cancer_ordering.output_forms import add_fields
        add_fields(self, patient, state=cancer_state)
        self.fields["document_ids"].queryset = Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("-created_at", "pk")
        self.fields['self_record_ids'].queryset = DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True)
        self.fields['glucose_record_ids'].queryset = GlucoseRecord.objects.filter(patient=patient, deleted_at__isnull=True)
        filenames = {str(row.pk): row.display_filename for row in self.fields["document_ids"].queryset}
        from apps.exports.pathology import choice_texts, selection_stamp
        material = review_reports(patient, actor=actor)
        self.pathology_stamp = selection_stamp(material)
        from apps.exports import molecular
        self.molecular_stamp = molecular.selection_stamp(material)
        reports = [row for row in material if any(field["usable"] for field in row["fields"])]
        field_texts = choice_texts(reports)
        field_texts.update(molecular.choice_texts(reports))
        self.fields["clinical_field_ids"].help_text += " 分子结果保留完整有序变异身份；药物项包含同组原药名、关联变异、原依据和方向/等级体系/日期状态，未说明者明确标记；不作为治疗建议。"
        self.fields["report_ids"].choices = [(row["id"], f"{filenames[row['document_id']]} · {row['title']}") for row in reports]
        self.fields["clinical_field_ids"].choices = [
            (field["id"], f"{filenames[row['document_id']]} · {field['field_label']}：{field_texts[field['id']]}")
            for row in reports for field in row["fields"] if field["usable"]
        ]
        from apps.exports.treatment_forms import add_derived_fields
        add_derived_fields(self, patient, actor=actor)
        from apps.lesions.output_forms import add_lesion_field
        add_lesion_field(self, patient, actor=actor)
        from apps.cloud_imaging.output_forms import add_cloud_field
        add_cloud_field(self, patient, actor)

    def clean(self):
        from apps.cancer_ordering.output_forms import clean_selection
        return clean_selection(self, super().clean())

    def selection(self):
        data = self.cleaned_data
        selection = {"document_ids": [str(row.pk) for row in data["document_ids"]], "sections": data["sections"],
                     "self_record_ids": [str(row.pk) for row in data['self_record_ids']],
                     "glucose_record_ids": [str(row.pk) for row in data['glucose_record_ids']]}
        for key in ("report_ids", "clinical_field_ids"):
            if data[key] or data['custom_reports' if key == 'report_ids' else 'custom_clinical_fields']:
                selection[key] = data[key]
        if data['lesion_ids']:
            selection['lesion_ids'] = data['lesion_ids']
        from apps.exports.treatment_forms import derived_selection
        from apps.exports.treatment import SELECTION_KEYS
        derived = derived_selection(data)
        if any(derived[key] for key in SELECTION_KEYS):
            selection.update({key: value for key, value in derived.items() if key not in SELECTION_KEYS or value})
        if data['cancer_candidate_ids'] or data['include_indicator_ordering']:
            selection.update({key: data[key] for key in ('cancer_candidate_ids', 'include_indicator_ordering',
                                                       'cancer_expected_fingerprint')})
        from apps.cloud_imaging.output_forms import cloud_selection
        selection.update(cloud_selection(self.cleaned_data))
        return selection
