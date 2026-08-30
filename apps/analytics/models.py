import uuid

from django.db import models


class AppendOnlyError(ValueError):
    pass


class ProductEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64)
    actor_hash = models.CharField(max_length=64, blank=True)
    properties = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["name", "-created_at"], name="analytics_event_recent")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("Product events are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Product events are append-only")

    def __str__(self):
        return f"Product event {self.pk}"
