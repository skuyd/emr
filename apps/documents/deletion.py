from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from functools import partial

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.analytics.events import record_product_event
from apps.operations.audit import record_audit_event
from apps.operations.models import TombstoneKind
from apps.operations.tombstones import record_deletion_tombstone

from .batches import refresh_batch_state
from .errors import ObjectNotFound, UploadDomainError
from .locking import lock_document_aggregate
from .models import BatchStatus, DocumentDeletionJob, UploadItem


RETRY_DELAYS = (60, 300, 1800, 7200, 21600)


class DeletionRequestUnavailable(ValueError):
    pass


class DeletionOutcome(str, Enum):
    PURGED = "PURGED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class DeletionResult:
    outcome: DeletionOutcome
    retry_delay: int | None = None


def _refresh_batch(batch, now):
    items = UploadItem.objects.filter(batch=batch)
    totals = items.aggregate(pages=Sum("page_count"), bytes=Sum("byte_size"))
    batch.file_count = items.count()
    batch.page_count = totals["pages"] or 0
    batch.byte_size = totals["bytes"] or 0
    if batch.file_count == 0:
        batch.status = BatchStatus.COMPLETED
        batch.completed_at = now
        batch.save(
            update_fields=["file_count", "page_count", "byte_size", "status", "completed_at", "updated_at"]
        )
    else:
        batch.save(update_fields=["file_count", "page_count", "byte_size", "updated_at"])
        refresh_batch_state(batch, now=now)


def request_document_deletion(patient, document_id, *, dispatch, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        document, batches = lock_document_aggregate(
            document_id, patient_id=patient.pk, include_references=True
        )
        if document is None or document.deleted_at is not None:
            raise DeletionRequestUnavailable()
        document_type = (
            document.parsing_versions.filter(active=True)
            .values_list("document_summary__document_type", flat=True)
            .first()
            or "UNKNOWN"
        )
        UploadItem.objects.filter(document=document).delete()
        document.deleted_at = now
        document.save(update_fields=["deleted_at", "updated_at"])
        record_deletion_tombstone(TombstoneKind.DOCUMENT, document.pk, now=now)
        job = DocumentDeletionJob.objects.create(document=document, object_key=document.original_object_key)
        record_product_event(
            "document_deleted",
            {"document_type": document_type},
            account_id=patient.account_id,
        )
        record_audit_event(
            patient.account_id,
            "document_deletion_requested",
            document.pk,
            "scheduled",
            "user_confirmed",
        )
        for batch in batches:
            _refresh_batch(batch, now)
        transaction.on_commit(partial(dispatch, job.pk))
    return job


def _retry(job, now):
    delay = RETRY_DELAYS[min(job.attempt_count, len(RETRY_DELAYS) - 1)]
    job.attempt_count = min(job.attempt_count + 1, 65535)
    job.next_attempt_at = now + timedelta(seconds=delay)
    job.error_code = "storage_unavailable"
    job.save(update_fields=["attempt_count", "next_attempt_at", "error_code", "updated_at"])
    return DeletionResult(DeletionOutcome.RETRY_SCHEDULED, delay)


def purge_document_deletion(job_id, object_store, *, now=None):
    now = now or timezone.now()
    job = DocumentDeletionJob.objects.select_related("document").filter(pk=job_id).first()
    if job is None:
        return DeletionResult(DeletionOutcome.NOT_FOUND)
    try:
        object_store.delete(job.object_key)
    except ObjectNotFound:
        pass
    except UploadDomainError:
        with transaction.atomic():
            locked = DocumentDeletionJob.objects.select_for_update().filter(pk=job.pk).first()
            if locked is None:
                return DeletionResult(DeletionOutcome.NOT_FOUND)
            return _retry(locked, now)

    with transaction.atomic():
        document, batches = lock_document_aggregate(job.document_id, include_references=True)
        if document is None:
            return DeletionResult(DeletionOutcome.NOT_FOUND)
        job = DocumentDeletionJob.objects.select_for_update().filter(pk=job.pk).first()
        if job is None:
            return DeletionResult(DeletionOutcome.NOT_FOUND)
        record_audit_event("system", "document_deletion_purged", document.pk, "succeeded")
        UploadItem.objects.filter(document=document).delete()
        document.delete()
        for batch in batches:
            if not batch.items.exists() and not batch.documents.exists():
                batch.delete()
            else:
                _refresh_batch(batch, now)
    return DeletionResult(DeletionOutcome.PURGED)


def due_document_deletions(*, now=None, limit=100):
    now = now or timezone.now()
    return tuple(
        DocumentDeletionJob.objects.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 1000))]
    )
