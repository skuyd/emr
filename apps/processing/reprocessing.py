from functools import partial

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.documents.batches import refresh_batch_state
from apps.documents.locking import lock_document_aggregate
from apps.documents.models import DocumentStatus, ProcessingRun, ProcessingStage
from apps.documents.services import INITIAL_PARSER_VERSION
from apps.operations.audit import record_audit_event
from apps.labs.quality import QUALITY_POLICY_VERSION


class ReprocessingUnavailable(ValueError):
    pass


def quality_refresh_required(document, active_version):
    return (
        document.status in {DocumentStatus.ORGANIZED, DocumentStatus.ORIGINAL_ONLY}
        and (
            active_version is None
            or active_version.diagnostics.get("quality_policy") != QUALITY_POLICY_VERSION
        )
    )


def queue_user_reprocessing(patient, document_id, *, dispatch, actor=None):
    with transaction.atomic():
        from apps.patients.access import authorize_patient, owner_actor
        access = authorize_patient(patient, owner_actor(patient, actor), "write", lock=True)
        document, batches = lock_document_aggregate(document_id, patient_id=patient.pk)
        if document is None or document.deleted_at is not None:
            raise ReprocessingUnavailable()
        active_version = document.parsing_versions.filter(active=True).first()
        if document.status != DocumentStatus.PROCESSING_FAILED and not quality_refresh_required(document, active_version):
            raise ReprocessingUnavailable()
        if document.processing_runs.filter(
            stage__in=(
                ProcessingStage.QUEUED,
                ProcessingStage.PREPARING,
                ProcessingStage.OCR,
                ProcessingStage.CLASSIFYING,
                ProcessingStage.EXTRACTING,
                ProcessingStage.INDEXING,
            )
        ).exists():
            raise ReprocessingUnavailable()
        latest_attempt = document.processing_runs.aggregate(value=Max("attempt_number"))["value"] or 0
        attempt_number = latest_attempt + 1
        task_type = f"USER_RETRY_{attempt_number}"
        try:
            with transaction.atomic():
                run = ProcessingRun.objects.create(
                    document=document,
                    requested_by=access.actor,
                    access_revision=access.membership.revision,
                    parser_version=INITIAL_PARSER_VERSION,
                    task_type=task_type,
                    idempotency_key=f"{document.pk}:{INITIAL_PARSER_VERSION}:{task_type}",
                    attempt_number=attempt_number,
                    stage=ProcessingStage.QUEUED,
                )
        except IntegrityError:
            raise ReprocessingUnavailable() from None
        document.status = DocumentStatus.PROCESSING
        document.save(update_fields=["status", "updated_at"])
        batch = batches[0]
        refresh_batch_state(batch)
        record_audit_event(
            access.actor.pk,
            "processing_requeued",
            document.pk,
            "scheduled",
            "user_retry",
        )
        transaction.on_commit(partial(dispatch, run.pk))
    return run


def invalidate_member_reprocessing(patient, account_id):
    """Fence retries under the Patient guard already held by access revocation."""
    stages = [ProcessingStage.QUEUED, ProcessingStage.PREPARING, ProcessingStage.OCR,
              ProcessingStage.CLASSIFYING, ProcessingStage.EXTRACTING, ProcessingStage.INDEXING]
    document_ids = ProcessingRun.objects.filter(
        document__patient=patient, requested_by_id=account_id, task_type__startswith="USER_RETRY_",
        stage__in=stages,
    ).order_by("document_id").values_list("document_id", flat=True)
    for document_id in list(document_ids):
        document, batches = lock_document_aggregate(document_id, patient_id=patient.pk)
        now = timezone.now()
        document.processing_runs.filter(requested_by_id=account_id, task_type__startswith="USER_RETRY_", stage__in=stages).update(
            stage=ProcessingStage.FAILED, lease_token=None, finished_at=now, next_retry_at=None,
            error_code="access_revoked", updated_at=now,
        )
        current = document.processing_runs.filter(is_current=True).first()
        document.status = (DocumentStatus.ORGANIZED if current and current.stage == ProcessingStage.SUCCEEDED
                           else DocumentStatus.ORIGINAL_ONLY if current else DocumentStatus.PROCESSING_FAILED)
        document.save(update_fields=["status", "updated_at"])
        for batch in batches:
            refresh_batch_state(batch)
