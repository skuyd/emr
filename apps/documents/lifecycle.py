"""Reversible trash and the transition into irreversible deletion.

Lock order matches upload finalization: patient, ordered batches, document, runs.
Only permanent deletion creates a tombstone or a physical cleanup job.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.labs.review import revoke_document_reviews
from apps.operations.audit import record_audit_event
from apps.patients.models import Patient

from .deletion import DeletionRequestUnavailable, _refresh_batch, request_document_deletion
from .locking import lock_document_aggregate
from .models import Document, DocumentStatus, ProcessingStage, UploadItem
from .quotas import QuotaExceeded, QuotaProposal, check_upload_quota


class LifecycleUnavailable(ValueError):
    pass


def _now(now):
    value = now or timezone.now()
    if timezone.is_naive(value):
        raise ValueError("Lifecycle timestamps must be timezone-aware")
    return value


def _owned_document(patient, document_id):
    document, batches = lock_document_aggregate(document_id, patient_id=patient.pk, include_references=True)
    if document is None or not Patient.objects.filter(pk=patient.pk, account__is_active=True).exists():
        raise LifecycleUnavailable("资料不可用。")
    return document, batches


def fence_processing(document, now):
    document.processing_runs.exclude(stage__in=[
        ProcessingStage.SUCCEEDED, ProcessingStage.NO_STRUCTURED_RESULT, ProcessingStage.FAILED,
    ]).update(
        stage=ProcessingStage.FAILED, lease_token=None, finished_at=now, next_retry_at=None,
        error_code="document_unavailable", updated_at=now,
    )
    if document.status == DocumentStatus.PROCESSING:
        current = document.processing_runs.filter(is_current=True).first()
        document.status = (
            DocumentStatus.ORGANIZED if current and current.stage == ProcessingStage.SUCCEEDED
            else DocumentStatus.ORIGINAL_ONLY if current else DocumentStatus.PROCESSING_FAILED
        )
        document.save(update_fields=["status", "updated_at"])


def move_to_trash(patient, document_id, *, now=None):
    with transaction.atomic():
        document, batches = _owned_document(patient, document_id)
        now = _now(now)
        if document.deleted_at is not None:
            raise LifecycleUnavailable("资料已不在正常列表中。")
        document.deleted_at = document.trashed_at = now
        document.trash_expires_at = now + timedelta(days=30)
        document.lifecycle_revision += 1
        document.save(update_fields=["deleted_at", "trashed_at", "trash_expires_at", "lifecycle_revision", "updated_at"])
        fence_processing(document, now)
        revoke_document_reviews(document, actor=patient.account, action="DOCUMENT_TRASHED")
        UploadItem.objects.filter(document=document).delete()
        for batch in batches:
            _refresh_batch(batch, now)
        record_audit_event(patient.account_id, "document_trashed", document.pk, "succeeded", "recoverable")
    return document


def restore_document(patient, document_id, *, now=None):
    with transaction.atomic():
        document, _batches = _owned_document(patient, document_id)
        now = _now(now)
        if document.trashed_at is None or hasattr(document, "deletion_job"):
            raise LifecycleUnavailable("资料已进入永久删除流程，无法恢复。")
        if document.trash_expires_at <= now:
            raise LifecycleUnavailable("30 天保留期已结束，无法恢复。")
        if Document.objects.filter(patient=patient, sha256=document.sha256, deleted_at__isnull=True).exists():
            raise LifecycleUnavailable("已存在相同内容的正常资料。请先处理该副本，两份原件均已保留。")
        try:
            check_upload_quota(patient, QuotaProposal(documents=1, document_pages=document.page_count))
        except QuotaExceeded as error:
            raise LifecycleUnavailable("恢复后将超出当前资料配额，请先整理其他资料。") from error
        document.deleted_at = document.trashed_at = document.trash_expires_at = None
        document.lifecycle_revision += 1
        document.save(update_fields=["deleted_at", "trashed_at", "trash_expires_at", "lifecycle_revision", "updated_at"])
        record_audit_event(patient.account_id, "document_restored", document.pk, "succeeded")
    return document


def permanently_delete_from_trash(patient, document_id, *, dispatch, now=None):
    with transaction.atomic():
        document, _batches = _owned_document(patient, document_id)
        now = _now(now)
        if document.trashed_at is None:
            raise LifecycleUnavailable("回收站资料不可用。")
        return request_document_deletion(patient, document.pk, dispatch=dispatch, now=now)


def expire_trash(*, dispatch, now=None, limit=100):
    now = _now(now)
    candidates = Document.objects.filter(trash_expires_at__lte=now).order_by("trash_expires_at", "pk")
    identities = tuple(candidates.values_list("pk", "patient_id")[:max(1, min(int(limit), 1000))])
    expired = []
    for document_id, patient_id in identities:
        with transaction.atomic():
            document, _batches = lock_document_aggregate(document_id, patient_id=patient_id, include_references=True)
            if document is None or document.trash_expires_at is None or document.trash_expires_at > now:
                continue
            # Re-check under the same lock used by restoration before creating a cleanup job.
            try:
                request_document_deletion(document.patient, document.pk, dispatch=dispatch, now=now)
            except DeletionRequestUnavailable:
                continue
            expired.append(document.pk)
    return tuple(expired)
