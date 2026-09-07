import uuid

from django.db import models
from django.utils import timezone


class AppendOnlyAuditError(ValueError):
    pass


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_hash = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    target_hash = models.CharField(max_length=64)
    result = models.CharField(max_length=16)
    reason_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["action", "-created_at"], name="operations_audit_recent")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyAuditError("Audit events are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyAuditError("Audit events are append-only")

    def __str__(self):
        return f"Audit event {self.pk}"


class TombstoneKind(models.TextChoices):
    ACCOUNT = "ACCOUNT", "Account"
    DOCUMENT = "DOCUMENT", "Document"
    PATIENT = "PATIENT", "Patient"


class DeletionTombstone(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=TombstoneKind.choices)
    target_hash = models.CharField(max_length=64)
    requested_at = models.DateTimeField(default=timezone.now)
    signature = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "target_hash"],
                name="operations_unique_tombstone",
            )
        ]
        indexes = [
            models.Index(fields=["kind", "created_at"], name="operations_tombstone_export")
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyAuditError("Deletion tombstones are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyAuditError("Deletion tombstones are append-only")

    def __str__(self):
        return f"Deletion tombstone {self.pk}"


class SupportAccessGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operator = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="support_access_grants",
    )
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="support_access_grants",
    )
    reason_code = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["operator", "patient", "expires_at"],
                name="operations_support_grant",
            )
        ]

    def __str__(self):
        return f"Support access grant {self.pk}"


class DictionaryRelease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=64, unique=True)
    content_hash = models.CharField(max_length=64, unique=True)
    artifact_name = models.CharField(max_length=128)
    indicator_count = models.PositiveSmallIntegerField()
    active = models.BooleanField(default=False)
    published_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField(default=dict, blank=True)
    rules = models.JSONField(default=list, blank=True)
    rules_hash = models.CharField(max_length=64, blank=True)
    release_hash = models.CharField(max_length=64, blank=True)
    candidate_reviews = models.JSONField(default=list, blank=True)
    regression_report = models.JSONField(default=dict, blank=True)
    diff = models.JSONField(default=dict, blank=True)
    published_by = models.ForeignKey("accounts.Account", null=True, blank=True, on_delete=models.SET_NULL)
    previous_release = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["active"],
                condition=models.Q(active=True),
                name="operations_one_active_dictionary",
            )
        ]
        indexes = [models.Index(fields=["active", "-published_at"], name="operations_dictionary_active")]

    def __str__(self):
        return f"Dictionary release {self.version}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            immutable = ("version", "content_hash", "artifact_name", "indicator_count", "payload", "rules", "rules_hash",
                         "release_hash", "candidate_reviews", "regression_report", "diff", "previous_release_id",
                         "published_by_id", "published_at")
            previous = type(self).objects.filter(pk=self.pk).values(*immutable).first()
            if previous and any(previous[name] != getattr(self, name) for name in immutable):
                from django.core.exceptions import ValidationError

                raise ValidationError("已发布字典不可修改，请发布新的版本。")
        return super().save(*args, **kwargs)


class DictionaryPublicationLock(models.Model):
    """A permanent row serializes first publication as well as later pointer switches."""

    key = models.CharField(primary_key=True, max_length=16, default="dictionary")


class DictionaryEvaluationEvent(models.Model):
    """Retain each publication/rollback evaluation without rewriting release history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(DictionaryRelease, on_delete=models.PROTECT, related_name='evaluation_events')
    actor = models.ForeignKey('accounts.Account', null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=16, choices=[('PUBLISH', '发布'), ('ROLLBACK', '回退'), ('BUNDLED', '内置字典')])
    report = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyAuditError('Dictionary evaluation events are append-only')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyAuditError('Dictionary evaluation events are append-only')


class OperationalMetricSeries(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    label_key = models.CharField(max_length=512)
    labels = models.JSONField(default=dict)
    value_sum = models.FloatField(default=0)
    sample_count = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "label_key"], name="operations_unique_metric_series")
        ]

    def __str__(self):
        return f"Operational metric {self.name}"
