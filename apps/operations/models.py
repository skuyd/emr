import uuid

from django.db import models


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
