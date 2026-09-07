from functools import partial

from django.db import IntegrityError, transaction
from django.db.models import Max

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


def queue_user_reprocessing(patient, document_id, *, dispatch, actor=None, for_material_review=False):
    with transaction.atomic():
        document, batches = lock_document_aggregate(document_id, patient_id=patient.pk)
        if document is None or document.deleted_at is not None:
            raise ReprocessingUnavailable()
        active_version = document.parsing_versions.filter(active=True).first()
        if for_material_review and document.material_override != "KEEP_DOCUMENT":
            raise ReprocessingUnavailable()
        if (not for_material_review and document.status != DocumentStatus.PROCESSING_FAILED
                and not quality_refresh_required(document, active_version)):
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
        batch = batches[0]
        refresh_batch_state(batch)
        record_audit_event(
            getattr(actor, "pk", actor) if actor is not None else patient.account_id,
            "processing_requeued",
            document.pk,
            "scheduled",
            "user_retry",
        )
        transaction.on_commit(partial(dispatch, run.pk))
    return run
