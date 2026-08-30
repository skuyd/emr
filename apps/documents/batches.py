from dataclasses import dataclass
from functools import partial

from django.db import connection, transaction
from django.utils import timezone

from .models import BatchStatus, DocumentStatus, UploadItem, UploadItemStatus


@dataclass(frozen=True)
class BatchCounts:
    processing: int
    completed: int
    failed: int
    total: int

    @property
    def terminal(self):
        return self.total > 0 and self.processing == 0


class BatchTransactionRequired(RuntimeError):
    pass


def item_projection_status(item):
    if item.status != UploadItemStatus.CREATED:
        return item.status
    document = getattr(item, "document", None)
    return document.status if document is not None else UploadItemStatus.UPLOADING


def summarize_batch(batch, *, items=None):
    if items is None:
        items = list(UploadItem.objects.filter(batch=batch).select_related("document").order_by("ordinal", "pk"))
    processing = completed = failed = 0
    for item in items:
        status = item_projection_status(item)
        if status in {UploadItemStatus.PENDING, UploadItemStatus.UPLOADING, DocumentStatus.PROCESSING}:
            processing += 1
        elif status in {UploadItemStatus.EXACT_DUPLICATE, DocumentStatus.ORGANIZED, DocumentStatus.ORIGINAL_ONLY}:
            completed += 1
        elif status in {UploadItemStatus.UPLOAD_FAILED, DocumentStatus.PROCESSING_FAILED}:
            failed += 1
        else:
            processing += 1
    total = len(items)
    return BatchCounts(processing=processing, completed=completed, failed=failed, total=total)


def refresh_batch_state(batch, *, now=None):
    """Persist the one authoritative ACTIVE/COMPLETED batch predicate."""

    if not connection.in_atomic_block:
        raise BatchTransactionRequired("refresh_batch_state requires transaction.atomic()")
    counts = summarize_batch(batch)
    target = BatchStatus.COMPLETED if counts.terminal else BatchStatus.ACTIVE
    changed = batch.status != target
    batch.status = target
    if target == BatchStatus.COMPLETED:
        if batch.completed_at is None:
            batch.completed_at = now or timezone.now()
            changed = True
    elif batch.completed_at is not None:
        batch.completed_at = None
        changed = True
    if changed:
        batch.save(update_fields=["status", "completed_at", "updated_at"])
    if changed and target == BatchStatus.COMPLETED:
        from apps.notifications.services import safe_create_task_notification

        transaction.on_commit(partial(safe_create_task_notification, batch.pk))
    return counts
