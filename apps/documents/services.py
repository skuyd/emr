from dataclasses import dataclass
from enum import Enum
import logging
import re
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Sum

from .deduplication import find_exact_duplicate
from .errors import UploadDomainError
from .inspection import InspectedFile
from .models import (
    BatchStatus,
    Document,
    DocumentPage,
    DocumentStatus,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from .quotas import QuotaExceeded, QuotaProposal, check_upload_quota, lock_patient_quota
from .storage import StagedObject


logger = logging.getLogger(__name__)

INITIAL_PARSER_VERSION = "phr-v1"
INITIAL_TASK_TYPE = "INITIAL_PARSE"
_RUN_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,64}")


class UploadOutcomeKind(str, Enum):
    CREATED = "CREATED"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"


@dataclass(frozen=True)
class UploadOutcome:
    kind: UploadOutcomeKind
    document_id: UUID
    item_id: UUID
    processing_run_id: UUID | None
    saved: bool = True


class UploadFinalizationError(UploadDomainError):
    pass


class UploadResourceNotFound(UploadFinalizationError):
    code = "upload_not_found"


class UploadStateConflict(UploadFinalizationError):
    code = "upload_state_conflict"


class ArtifactMismatch(UploadFinalizationError):
    code = "artifact_mismatch"


class UploadCompensationFailed(UploadFinalizationError):
    code = "upload_compensation_failed"


class InvalidProcessingIdentity(UploadFinalizationError):
    code = "invalid_processing_identity"


def _run_idempotency_key(document_id, parser_version, task_type):
    if (
        not isinstance(parser_version, str)
        or not isinstance(task_type, str)
        or _RUN_COMPONENT.fullmatch(parser_version) is None
        or _RUN_COMPONENT.fullmatch(task_type) is None
    ):
        raise InvalidProcessingIdentity()
    return f"{document_id}:{parser_version}:{task_type}"


def _validate_artifacts(inspected, staged):
    if not isinstance(inspected, InspectedFile) or inspected.closed or not isinstance(staged, StagedObject):
        raise ArtifactMismatch()
    if inspected.byte_size != staged.byte_size or inspected.sha256 != staged.sha256:
        raise ArtifactMismatch()
    if inspected.page_count <= 0 or len(inspected.dimensions) != inspected.page_count:
        raise ArtifactMismatch()


def _batch_proposal(batch, item, inspected):
    other_items = batch.items.exclude(pk=item.pk)
    totals = other_items.aggregate(pages=Sum("page_count"), bytes=Sum("byte_size"))
    return QuotaProposal(
        batch_files=batch.items.count(),
        batch_pages=(totals["pages"] or 0) + inspected.page_count,
        storage_bytes=inspected.byte_size,
        documents=1,
        document_pages=inspected.page_count,
    ), (totals["bytes"] or 0) + inspected.byte_size


def _enforce_batch_limits(quota, proposal):
    if proposal.batch_files > quota.batch_file_limit:
        raise QuotaExceeded("batch_file_limit")
    if proposal.batch_pages > quota.batch_page_limit:
        raise QuotaExceeded("batch_page_limit")


def _update_batch_and_item(*, batch, item, inspected, batch_proposal, batch_bytes, status, document):
    item.byte_size = inspected.byte_size
    item.page_count = inspected.page_count
    item.status = status
    item.error_code = ""
    item.document = document
    item.save(update_fields=["byte_size", "page_count", "status", "error_code", "document", "updated_at"])
    batch.file_count = batch_proposal.batch_files
    batch.page_count = batch_proposal.batch_pages
    batch.byte_size = batch_bytes
    batch.save(update_fields=["file_count", "page_count", "byte_size", "updated_at"])


def _orientation(width, height):
    if width == height:
        return "SQUARE"
    return "LANDSCAPE" if width > height else "PORTRAIT"


def _create_pages(document, inspected):
    DocumentPage.objects.bulk_create(
        [
            DocumentPage(
                document=document,
                page_number=index,
                width=width,
                height=height,
                orientation=_orientation(width, height),
            )
            for index, (width, height) in enumerate(inspected.dimensions, start=1)
        ]
    )


def _safe_dispatch(dispatch, run_id):
    try:
        dispatch(str(run_id))
    except Exception:
        logger.warning(
            "processing_dispatch_failed",
            extra={"run_id": str(run_id), "error_code": "broker_unavailable"},
        )


def _safe_delete_staging(store, staged):
    try:
        store.delete(staged)
    except Exception:
        logger.warning("staging_cleanup_deferred", extra={"error_code": "staging_cleanup_deferred"})


def _terminal_outcome(item, inspected, patient, parser_version, task_type):
    if item.status not in {UploadItemStatus.CREATED, UploadItemStatus.EXACT_DUPLICATE} or not item.document_id:
        return None
    document = (
        Document.objects.select_for_update()
        .filter(pk=item.document_id, patient_id=patient.pk, deleted_at__isnull=True)
        .first()
    )
    if document is None or (
        document.sha256 != inspected.sha256
        or document.byte_size != inspected.byte_size
        or document.page_count != inspected.page_count
        or document.content_type != inspected.content_type
    ):
        raise UploadStateConflict()
    if item.status == UploadItemStatus.CREATED:
        idempotency_key = _run_idempotency_key(document.pk, parser_version, task_type)
        run_id = (
            ProcessingRun.objects.filter(
                document_id=item.document_id,
                parser_version=parser_version,
                task_type=task_type,
                idempotency_key=idempotency_key,
            )
            .values_list("pk", flat=True)
            .first()
        )
        if run_id is None:
            raise UploadStateConflict()
        return UploadOutcome(UploadOutcomeKind.CREATED, item.document_id, item.pk, run_id)
    return UploadOutcome(UploadOutcomeKind.EXACT_DUPLICATE, item.document_id, item.pk, None)


def _record_duplicate(*, duplicate, batch, item, inspected, proposal, batch_bytes, store, staged):
    _update_batch_and_item(
        batch=batch,
        item=item,
        inspected=inspected,
        batch_proposal=proposal,
        batch_bytes=batch_bytes,
        status=UploadItemStatus.EXACT_DUPLICATE,
        document=duplicate,
    )
    transaction.on_commit(lambda: _safe_delete_staging(store, staged))
    return UploadOutcome(UploadOutcomeKind.EXACT_DUPLICATE, duplicate.pk, item.pk, None)


def finalize_upload(
    patient,
    batch_id,
    item_id,
    inspected,
    staged,
    store,
    dispatch=None,
    *,
    parser_version=INITIAL_PARSER_VERSION,
    task_type=INITIAL_TASK_TYPE,
):
    """Atomically bind one verified, durable original to an archive document."""

    _validate_artifacts(inspected, staged)
    _run_idempotency_key(item_id, parser_version, task_type)
    if not UploadItem.objects.filter(
        pk=item_id,
        batch_id=batch_id,
        batch__patient_id=patient.pk,
    ).exists():
        raise UploadResourceNotFound()
    promoted = None
    try:
        with transaction.atomic():
            quota = lock_patient_quota(patient)
            batch = (
                UploadBatch.objects.select_for_update()
                .filter(pk=batch_id, patient_id=patient.pk)
                .first()
            )
            if batch is None:
                raise UploadResourceNotFound()
            item = UploadItem.objects.select_for_update().filter(pk=item_id, batch=batch).first()
            if item is None:
                raise UploadResourceNotFound()

            terminal = _terminal_outcome(item, inspected, patient, parser_version, task_type)
            if terminal is not None:
                transaction.on_commit(lambda: _safe_delete_staging(store, staged))
                return terminal
            if batch.status != BatchStatus.ACTIVE or item.status not in {
                UploadItemStatus.PENDING,
                UploadItemStatus.UPLOADING,
                UploadItemStatus.UPLOAD_FAILED,
            }:
                raise UploadStateConflict()

            proposal, batch_bytes = _batch_proposal(batch, item, inspected)
            _enforce_batch_limits(quota, proposal)
            duplicate = find_exact_duplicate(patient, inspected.sha256, lock=True)
            if duplicate is not None:
                return _record_duplicate(
                    duplicate=duplicate,
                    batch=batch,
                    item=item,
                    inspected=inspected,
                    proposal=proposal,
                    batch_bytes=batch_bytes,
                    store=store,
                    staged=staged,
                )

            check_upload_quota(patient, proposal, quota=quota)
            final_key = f"originals/{item.pk.hex}"
            promoted = store.promote_immutable(staged, final_key)

            try:
                with transaction.atomic():
                    document = Document.objects.create(
                        id=item.pk,
                        patient=patient,
                        batch=batch,
                        display_filename=item.display_filename,
                        content_type=inspected.content_type,
                        byte_size=inspected.byte_size,
                        page_count=inspected.page_count,
                        sha256=inspected.sha256,
                        original_object_key=promoted.key,
                        status=DocumentStatus.PROCESSING,
                    )
            except IntegrityError:
                duplicate = find_exact_duplicate(patient, inspected.sha256, lock=True)
                if duplicate is None:
                    raise
                store.compensate_promotion(promoted)
                promoted = None
                return _record_duplicate(
                    duplicate=duplicate,
                    batch=batch,
                    item=item,
                    inspected=inspected,
                    proposal=proposal,
                    batch_bytes=batch_bytes,
                    store=store,
                    staged=staged,
                )

            _create_pages(document, inspected)
            run = ProcessingRun.objects.create(
                document=document,
                parser_version=parser_version,
                task_type=task_type,
                idempotency_key=_run_idempotency_key(document.pk, parser_version, task_type),
                stage=ProcessingStage.QUEUED,
                is_current=False,
            )
            _update_batch_and_item(
                batch=batch,
                item=item,
                inspected=inspected,
                batch_proposal=proposal,
                batch_bytes=batch_bytes,
                status=UploadItemStatus.CREATED,
                document=document,
            )
            if dispatch is not None:
                transaction.on_commit(lambda: _safe_dispatch(dispatch, run.pk))
            outcome = UploadOutcome(UploadOutcomeKind.CREATED, document.pk, item.pk, run.pk)
        promoted = None
        return outcome
    except Exception:
        if promoted is not None:
            try:
                store.compensate_promotion(promoted)
            except UploadDomainError:
                raise UploadCompensationFailed() from None
        raise
