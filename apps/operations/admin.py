from django.contrib import admin

from .models import (
    AuditEvent,
    DeletionTombstone,
    DictionaryRelease,
    DictionaryEvaluationEvent,
    OperationalMetricSeries,
    SupportAccessGrant,
)


class ReadOnlyOperationsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyOperationsAdmin):
    list_display = ("id", "action", "result", "reason_code", "created_at")
    exclude = ("actor_hash", "target_hash")


@admin.register(DeletionTombstone)
class DeletionTombstoneAdmin(ReadOnlyOperationsAdmin):
    list_display = ("id", "kind", "requested_at", "created_at")
    exclude = ("target_hash", "signature")


@admin.register(DictionaryRelease)
class DictionaryReleaseAdmin(ReadOnlyOperationsAdmin):
    list_display = ("version", "indicator_count", "active", "published_at")
    exclude = ("content_hash", "artifact_name")


@admin.register(DictionaryEvaluationEvent)
class DictionaryEvaluationEventAdmin(ReadOnlyOperationsAdmin):
    list_display = ('release', 'action', 'created_at')


@admin.register(SupportAccessGrant)
class SupportAccessGrantAdmin(ReadOnlyOperationsAdmin):
    list_display = ("id", "reason_code", "expires_at", "revoked_at", "created_at")
    exclude = ("operator", "patient")


@admin.register(OperationalMetricSeries)
class OperationalMetricSeriesAdmin(ReadOnlyOperationsAdmin):
    list_display = ("name", "sample_count", "updated_at")
    exclude = ("labels", "label_key", "value_sum")
