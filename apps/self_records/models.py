import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class DailyRecord(models.Model):
    class Kind(models.TextChoices):
        WEIGHT = 'WEIGHT', '体重'
        TEMPERATURE = 'TEMPERATURE', '体温'
        SYMPTOM = 'SYMPTOM', '症状'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='daily_records')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='created_daily_records')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='updated_daily_records')
    creation_key = models.UUIDField()
    creation_fingerprint = models.CharField(max_length=64)
    original_data = models.JSONField()
    current_data = models.JSONField()
    kind = models.CharField(max_length=16, choices=Kind.choices)
    measured_at = models.DateTimeField()
    revision_number = models.PositiveIntegerField(default=0)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-measured_at', 'id']
        indexes = [models.Index(fields=['patient', 'deleted_at', '-measured_at'], name='self_records_patient_time')]
        constraints = [models.UniqueConstraint(fields=['patient', 'created_by', 'creation_key'], name='self_records_creation_unique')]


class DailyRecordRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(DailyRecord, on_delete=models.CASCADE, related_name='revisions')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16, choices=[('CORRECT', '更正'), ('DELETE', '删除'), ('UNDO', '撤销')])
    before = models.JSONField()
    after = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['record', 'sequence'], name='self_records_revision_unique')]


class DailyRecordExportSource(models.Model):
    job = models.ForeignKey('exports.ExportJob', on_delete=models.CASCADE, related_name='self_record_sources')
    record = models.ForeignKey(DailyRecord, on_delete=models.CASCADE, related_name='export_bindings')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'record'], name='self_records_export_unique')]


class DailyRecordShareSource(models.Model):
    share = models.ForeignKey('patients.PatientShare', on_delete=models.CASCADE, related_name='self_record_sources')
    record = models.ForeignKey(DailyRecord, on_delete=models.CASCADE, related_name='share_bindings')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['share', 'record'], name='self_records_share_unique')]
