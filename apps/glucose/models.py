import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class GlucoseRecord(models.Model):
    class SourceKind(models.TextChoices):
        LAB_REPORT = 'LAB_REPORT', '检验单'
        NURSING = 'NURSING', '护理原件核对录入'
        METER = 'METER', '血糖仪自测'
        MANUAL = 'MANUAL', '手动记录'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='glucose_records')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='created_glucose_records')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='updated_glucose_records')
    creation_key = models.UUIDField()
    creation_fingerprint = models.CharField(max_length=64)
    original_data = models.JSONField()
    current_data = models.JSONField()
    source_kind = models.CharField(max_length=16, choices=SourceKind.choices)
    source_document = models.ForeignKey('documents.Document', null=True, blank=True, on_delete=models.CASCADE, related_name='glucose_records')
    source_page = models.ForeignKey('documents.DocumentPage', null=True, blank=True, on_delete=models.CASCADE, related_name='glucose_records')
    source_parsing_version = models.ForeignKey('processing.ParsingVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='glucose_records')
    source_observation = models.ForeignKey('labs.LabObservation', null=True, blank=True, on_delete=models.SET_NULL, related_name='glucose_records')
    source_fingerprint = models.CharField(max_length=64, blank=True)
    measured_at = models.DateTimeField(null=True, blank=True)
    measured_date = models.DateField(null=True, blank=True)
    time_precision = models.CharField(max_length=10)
    revision_number = models.PositiveIntegerField(default=0)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-measured_date', '-measured_at', 'id']
        indexes = [models.Index(fields=['patient', 'deleted_at', '-measured_date'], name='glucose_patient_date')]
        constraints = [
            models.UniqueConstraint(fields=['patient', 'created_by', 'creation_key'], name='glucose_creation_unique'),
            models.UniqueConstraint(fields=['patient', 'source_observation'], name='glucose_observation_unique'),
        ]

    def clean(self):
        super().clean()
        sourced = self.source_kind in ('LAB_REPORT', 'NURSING')
        if sourced != bool(self.source_document_id) or sourced != bool(self.source_page_id):
            raise ValidationError('报告来源须绑定实际原件页；自测与手动记录不绑定报告。')
        if self.source_document_id:
            if self.source_document.patient_id != self.patient_id or self.source_page.document_id != self.source_document_id:
                raise ValidationError('血糖来源必须属于当前患者和对应原件。')
        if self.source_parsing_version_id and self.source_parsing_version.document_id != self.source_document_id:
            raise ValidationError('来源解析版本不属于对应原件。')
        if self.source_observation_id and (self.source_kind != 'LAB_REPORT'
                or self.source_observation.parsing_version_id != self.source_parsing_version_id
                or self.source_observation.document_page_id != self.source_page_id):
            raise ValidationError('来源观察项与对应原页或解析版本不符。')


class GlucoseRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(GlucoseRecord, on_delete=models.CASCADE, related_name='revisions')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16, choices=[('CORRECT', '更正'), ('DELETE', '删除'), ('UNDO', '撤销'), ('RECHECK', '来源重核')])
    before = models.JSONField()
    after = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['record', 'sequence'], name='glucose_revision_unique')]


class GlucoseExportSource(models.Model):
    job = models.ForeignKey('exports.ExportJob', on_delete=models.CASCADE, related_name='glucose_sources')
    record = models.ForeignKey(GlucoseRecord, on_delete=models.CASCADE, related_name='export_bindings')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'record'], name='glucose_export_unique')]


class GlucoseShareSource(models.Model):
    share = models.ForeignKey('patients.PatientShare', on_delete=models.CASCADE, related_name='glucose_sources')
    record = models.ForeignKey(GlucoseRecord, on_delete=models.CASCADE, related_name='share_bindings')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['share', 'record'], name='glucose_share_unique')]
