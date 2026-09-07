import hashlib
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured, RequestDataTooBig, SuspiciousOperation
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Prefetch, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import parse_etags
from django.views.decorators.http import require_GET, require_POST

from apps.analytics.events import record_product_event, size_bucket
from apps.core.decorators import patient_required
from apps.patients.access import authorize_patient, owner_actor
from django.core.exceptions import PermissionDenied
from apps.core.responses import protect_sensitive_html
from apps.processing.tasks import safe_enqueue_processing
from apps.processing.material_review import material_projection
from apps.processing.models import ParsingVersion

from ..backends import get_object_store
from ..batches import item_projection_status, refresh_batch_state, summarize_batch
from ..errors import InspectionError, StorageTransportError, UploadDomainError
from ..forms import BatchRequestError, parse_batch_request
from ..inspection import MAX_PDF_BYTES, inspect_upload
from ..models import UploadBatch, UploadItem, UploadItemStatus, sanitize_display_filename
from ..quotas import QuotaExceeded
from ..services import (
    ArtifactMismatch,
    UploadOutcomeKind,
    UploadResourceNotFound,
    UploadStateConflict,
    finalize_upload,
)
from ..throttling import UploadRateLimited, check_upload_rate


MAX_MULTIPART_BYTES = MAX_PDF_BYTES + 1024 * 1024


def _json(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _error(code, status):
    return _json({"error": {"code": code}}, status=status)


def _rate_limit(request):
    try:
        check_upload_rate(request, request.patient)
    except UploadRateLimited:
        response = _error("upload_rate_limited", 429)
        response["Retry-After"] = "60"
        return response
    return None


@patient_required
@require_GET
def upload_page(request):
    source = request.GET.get("source", "other")
    if source not in {"home", "records", "other"}:
        source = "other"
    if source == "other":
        referring_path = urlsplit(request.META.get("HTTP_REFERER", "")).path
        if referring_path == "/":
            source = "home"
        elif referring_path == "/records/":
            source = "records"
    record_product_event("upload_entry_clicked", {"source": source}, account_id=request.user.pk)
    return protect_sensitive_html(render(request, "documents/upload.html", {"current_section": "home"}))


@patient_required
@require_POST
def create_batch(request):
    limited = _rate_limit(request)
    if limited is not None:
        return limited
    if request.content_type != "application/json":
        return _error("unsupported_content_type", 415)
    try:
        candidates = parse_batch_request(request.body)
    except (BatchRequestError, RequestDataTooBig) as error:
        return _error(getattr(error, "code", "invalid_batch_request"), 400)

    with transaction.atomic():
        authorize_patient(request.patient, request.user, "write", lock=True)
        batch = UploadBatch.objects.create(patient=request.patient, created_by=request.user, file_count=len(candidates))
        items = []
        for candidate in candidates:
            item = UploadItem.objects.create(
                batch=batch,
                ordinal=candidate.ordinal,
                display_filename=candidate.display_filename,
                status=UploadItemStatus.PENDING if candidate.accepted else UploadItemStatus.UPLOAD_FAILED,
                error_code=candidate.error_code,
            )
            items.append((item, candidate))
        refresh_batch_state(batch)

        from apps.operations.audit import record_audit_event
        record_audit_event(request.user.pk, "upload_started", batch.pk, "succeeded", patient_id=request.patient.pk, resource_type="upload_batch")

    record_product_event(
        "upload_started",
        {"file_count": len(candidates), "total_size_bucket": "unknown"},
        account_id=request.user.pk,
    )

    return _json(
        {
            "batch_id": str(batch.pk),
            "items": [
                {
                    "ordinal": candidate.ordinal,
                    "item_id": str(item.pk),
                    "accepted": candidate.accepted,
                    "error_code": candidate.error_code or None,
                }
                for item, candidate in items
            ],
        },
        status=201,
    )


def _begin_upload(patient, batch_id, item_id, actor=None):
    with transaction.atomic():
        authorize_patient(patient, owner_actor(patient, actor), "write", lock=True)
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
        if item.status not in {UploadItemStatus.PENDING, UploadItemStatus.UPLOAD_FAILED} or item.document_id:
            raise UploadStateConflict()
        item.status = UploadItemStatus.UPLOADING
        item.error_code = ""
        item.save(update_fields=["status", "error_code", "updated_at"])
        refresh_batch_state(batch)
        return item


def _mark_failed(patient, batch_id, item_id, code):
    # System compensation for an already accepted upload attempt. Even if its
    # actor was revoked during I/O, clear the busy state without retaining data.
    with transaction.atomic():
        from apps.patients.models import Patient
        if not Patient.objects.select_for_update().filter(pk=patient.pk).exists():
            return
        batch = (
            UploadBatch.objects.select_for_update()
            .filter(pk=batch_id, patient_id=patient.pk)
            .first()
        )
        if batch is None:
            return
        item = UploadItem.objects.select_for_update().filter(pk=item_id, batch=batch).first()
        if item is None or item.document_id or item.status in {UploadItemStatus.CREATED, UploadItemStatus.EXACT_DUPLICATE}:
            return
        item.status = UploadItemStatus.UPLOAD_FAILED
        item.error_code = code
        item.save(update_fields=["status", "error_code", "updated_at"])
        refresh_batch_state(batch)


def _cleanup_staged(store, staged):
    if store is None or staged is None:
        return
    try:
        store.delete(staged)
    except Exception:
        pass


def _inspection_status(code):
    return 413 if code == "file_too_large" else 400


@patient_required
@require_POST
def upload_item_content(request, batch_id, item_id):
    limited = _rate_limit(request)
    if limited is not None:
        return limited
    try:
        item = _begin_upload(request.patient, batch_id, item_id, request.user)
    except UploadResourceNotFound:
        raise Http404
    except UploadStateConflict as error:
        return _error(error.code, 409)
    except DatabaseError:
        return _error("upload_service_unavailable", 503)

    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > MAX_MULTIPART_BYTES:
        _mark_failed(request.patient, batch_id, item_id, "file_too_large")
        return _error("file_too_large", 413)
    if not request.content_type.startswith("multipart/form-data"):
        _mark_failed(request.patient, batch_id, item_id, "invalid_upload_request")
        return _error("unsupported_content_type", 415)

    try:
        uploaded_files = request.FILES
    except (RequestDataTooBig, SuspiciousOperation):
        _mark_failed(request.patient, batch_id, item_id, "invalid_upload_request")
        return _error("invalid_upload_request", 400)
    if len(uploaded_files) != 1 or "file" not in uploaded_files or len(uploaded_files.getlist("file")) != 1:
        _mark_failed(request.patient, batch_id, item_id, "invalid_upload_request")
        return _error("invalid_upload_request", 400)
    uploaded = uploaded_files["file"]
    try:
        safe_name = sanitize_display_filename(uploaded.name)
    except (TypeError, ValueError):
        _mark_failed(request.patient, batch_id, item_id, "invalid_file_metadata")
        return _error("invalid_file_metadata", 400)
    store = None
    staged = None
    try:
        with inspect_upload(uploaded, uploaded.name) as inspected:
            store = get_object_store()
            with inspected.open() as source:
                staged = store.put_staging(
                    source,
                    expected_size=inspected.byte_size,
                    expected_sha256=inspected.sha256,
                )
            outcome = finalize_upload(
                request.patient,
                batch_id,
                item_id,
                inspected,
                staged,
                store,
                actor=request.user,
                display_filename=safe_name,
                dispatch=safe_enqueue_processing if settings.PROCESSING_DISPATCH_ON_UPLOAD else None,
            )
        staged = None
    except InspectionError as error:
        _mark_failed(request.patient, batch_id, item_id, error.code)
        return _error(error.code, _inspection_status(error.code))
    except QuotaExceeded as error:
        _cleanup_staged(store, staged)
        _mark_failed(request.patient, batch_id, item_id, error.code)
        return _error(error.code, 400)
    except PermissionDenied:
        _cleanup_staged(store, staged)
        _mark_failed(request.patient, batch_id, item_id, "access_revoked")
        raise
    except UploadResourceNotFound:
        _cleanup_staged(store, staged)
        raise Http404
    except UploadStateConflict as error:
        _cleanup_staged(store, staged)
        return _error(error.code, 409)
    except (StorageTransportError, ImproperlyConfigured):
        _cleanup_staged(store, staged)
        _mark_failed(request.patient, batch_id, item_id, "storage_unavailable")
        return _error("storage_unavailable", 503)
    except (ArtifactMismatch, DatabaseError, UploadDomainError):
        _cleanup_staged(store, staged)
        _mark_failed(request.patient, batch_id, item_id, "upload_service_unavailable")
        return _error("upload_service_unavailable", 503)

    status = 201 if outcome.kind == UploadOutcomeKind.CREATED else 200
    if outcome.kind == UploadOutcomeKind.CREATED:
        event_format = {
            "application/pdf": "pdf",
            "image/jpeg": "jpeg",
            "image/png": "png",
            "image/heic": "heic",
        }[inspected.content_type]
        record_product_event(
            "file_upload_succeeded",
            {
                "format": event_format,
                "size_bucket": size_bucket(inspected.byte_size),
                "duration_bucket": "unknown",
            },
            account_id=request.user.pk,
        )
    return _json(
        {
            "outcome": outcome.kind.value,
            "saved": outcome.saved,
            "item_id": str(outcome.item_id),
            "document_id": str(outcome.document_id),
            "page_count": inspected.page_count,
            "possible_duplicate": outcome.possible_duplicate_document_id is not None,
            "status": "PROCESSING" if outcome.kind == UploadOutcomeKind.CREATED else "EXACT_DUPLICATE",
        },
        status=status,
    )


@patient_required
@require_POST
def remove_upload_item(request, batch_id, item_id):
    with transaction.atomic():
        authorize_patient(request.patient, request.user, "write", lock=True)
        batch = (
            UploadBatch.objects.select_for_update()
            .filter(pk=batch_id, patient_id=request.patient.pk)
            .first()
        )
        if batch is None:
            raise Http404
        item = UploadItem.objects.select_for_update().filter(pk=item_id, batch=batch).first()
        if item is None:
            raise Http404
        if item.status not in {UploadItemStatus.PENDING, UploadItemStatus.UPLOAD_FAILED} or item.document_id:
            return _error("upload_state_conflict", 409)
        from apps.operations.audit import record_audit_event
        record_audit_event(request.user.pk, "upload_removed", item.pk, "succeeded", patient_id=request.patient.pk, resource_type="upload_item")
        item.delete()
        remaining = batch.items.count()
        if remaining == 0:
            batch.delete()
            return _json({"removed": True, "batch_deleted": True})
        totals = batch.items.aggregate(pages=Sum("page_count"), bytes=Sum("byte_size"))
        batch.file_count = remaining
        batch.page_count = totals["pages"] or 0
        batch.byte_size = totals["bytes"] or 0
        batch.save(update_fields=["file_count", "page_count", "byte_size", "updated_at"])
        refresh_batch_state(batch)
    return _json({"removed": True, "batch_deleted": False})


def _batch_etag(batch, items):
    parts = [str(batch.pk), batch.status, batch.updated_at.isoformat(), str(batch.completed_at or "")]
    for item in items:
        parts.extend([str(item.pk), item.status, item.error_code, item.updated_at.isoformat()])
        if item.document_id:
            parts.extend([item.document.status, item.document.updated_at.isoformat()])
            parts.extend(str(version.pk) for version in item.document.material_versions)
    return f'"{hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()}"'


@patient_required
@require_GET
def batch_status(request, batch_id):
    batch = UploadBatch.objects.filter(pk=batch_id, patient_id=request.patient.pk).first()
    if batch is None:
        raise Http404
    items = list(batch.items.select_related("document").prefetch_related(
        Prefetch("document__parsing_versions", queryset=ParsingVersion.objects.filter(active=True), to_attr="material_versions")
    ).order_by("ordinal", "pk"))
    etag = _batch_etag(batch, items)
    if etag in parse_etags(request.headers.get("If-None-Match", "")):
        response = HttpResponse(status=304)
        response["ETag"] = etag
        response["Cache-Control"] = "no-store"
        return response
    counts = summarize_batch(batch, items=items)
    response = _json(
        {
            "batch_id": str(batch.pk),
            "terminal": counts.terminal,
            "counts": {
                "processing": counts.processing,
                "completed": counts.completed,
                "failed": counts.failed,
                "total": counts.total,
            },
            "items": [
                {
                    "ordinal": item.ordinal,
                    "item_id": str(item.pk),
                    "status": str(item_projection_status(item)),
                    "error_code": item.error_code or None,
                    "document_id": str(item.document_id) if item.document_id else None,
                    "page_count": item.page_count or None,
                    "material": material_projection(item.document) if item.document_id else None,
                }
                for item in items
            ],
        }
    )
    response["ETag"] = etag
    return response
