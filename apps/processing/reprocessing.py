from functools import partial

from django.db import IntegrityError, transaction
from django.db.models import Max

from apps.documents.batches import refresh_batch_state
from apps.documents.models import Document, DocumentStatus, ProcessingRun, ProcessingStage, UploadBatch
from apps.documents.services import INITIAL_PARSER_VERSION
from apps.operations.audit import record_audit_event


class ReprocessingUnavailable(ValueError):
    pass


def queue_user_reprocessing(patient, document_id, *, dispatch):
    with transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id, patient=patient, deleted_at__isnull=True)
            .first()
        )
        if document is None or document.status != DocumentStatus.PROCESSING_FAILED:
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
        batch = UploadBatch.objects.select_for_update().get(pk=document.batch_id)
        refresh_batch_state(batch)
        record_audit_event(
            patient.account_id,
            "processing_requeued",
            document.pk,
            "scheduled",
            "user_retry",
        )
        transaction.on_commit(partial(dispatch, run.pk))
    return run
