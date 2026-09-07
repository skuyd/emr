import uuid

from django.db import models
from django.db.models import Q
from django.conf import settings


class ExportStatus(models.TextChoices):
    PREVIEW = "PREVIEW", "待确认"
    QUEUED = "QUEUED", "等待生成"
    GENERATING = "GENERATING", "生成中"
    READY = "READY", "可下载"
    FAILED = "FAILED", "生成失败"
    CANCELLED = "CANCELLED", "已取消"
    INVALIDATED = "INVALIDATED", "内容已失效"
    EXPIRED = "EXPIRED", "已过期"


class ExportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="exports")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    access_revision = models.PositiveIntegerField(default=0)
    session_digest = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    snapshot_digest = models.CharField(max_length=64)
    options = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=ExportStatus, default=ExportStatus.PREVIEW)
    generation = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    object_key = models.CharField(max_length=512, blank=True)
    filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    failures = models.JSONField(default=list)
    cleanup_pending = models.BooleanField(default=False)
    cleanup_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.requested_by_id:
            self.requested_by_id = self.patient.account_id
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=["status", "expires_at"], name="exports_status_expiry"),
            models.Index(fields=["cleanup_pending", "cleanup_retry_at"], name="exports_cleanup_due"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(status="READY") | (
                Q(completed_at__isnull=False) & ~Q(object_key="") & ~Q(sha256="") & Q(byte_size__gt=0)
            ), name="exports_ready_has_file"),
        ]


class ExportSource(models.Model):
    job = models.ForeignKey(ExportJob, on_delete=models.CASCADE, related_name="source_bindings")
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, related_name="export_bindings")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "document"], name="exports_source_unique")]


class ExportAttempt(models.Model):
    """Object identities are durable before any external write, including staging."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(ExportJob, on_delete=models.CASCADE, related_name="attempts")
    generation = models.PositiveIntegerField()
    object_key = models.CharField(max_length=512, unique=True)
    staging_key = models.CharField(max_length=512, unique=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "generation"], name="exports_attempt_generation")]
