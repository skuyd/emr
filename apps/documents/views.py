import hashlib
import logging
import re
from contextlib import closing
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured, RequestDataTooBig, SuspiciousOperation, ValidationError
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import parse_etags
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.analytics.events import count_bucket, record_product_event, size_bucket
from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.labs.trends import trend_view
from apps.operations.audit import record_audit_event
from apps.operations.metrics import safe_record_metric
from apps.processing.models import SourceEvidence
from apps.processing.reprocessing import ReprocessingUnavailable, queue_user_reprocessing
from apps.processing.tasks import safe_enqueue_processing

from .archive import records_context
from .backends import get_object_store
from .batches import item_projection_status, refresh_batch_state, summarize_batch
from .errors import InspectionError, ObjectNotFound, StorageTransportError, UploadDomainError
from .detail import document_detail_context, document_detail_queryset
from .deletion import DeletionRequestUnavailable, request_document_deletion
from .forms import BatchRequestError, parse_batch_request
from .inspection import MAX_PDF_BYTES, inspect_upload
from .models import Document, InaccuracyFeedback, UploadBatch, UploadItem, UploadItemStatus, sanitize_display_filename
from .quotas import QuotaExceeded
from .previews import PreviewUnavailable, render_page, render_thumbnail_sheet
from .services import (
    ArtifactMismatch,
    UploadOutcomeKind,
    UploadResourceNotFound,
    UploadStateConflict,
    finalize_upload,
)
from .throttling import UploadRateLimited, check_upload_rate
from .tasks import safe_enqueue_document_deletion


MAX_MULTIPART_BYTES = MAX_PDF_BYTES + 1024 * 1024
logger = logging.getLogger(__name__)
_OPAQUE_ORIGINAL_FILENAMES = {
    "application/pdf": "original.pdf",
    "image/jpeg": "original.jpg",
    "image/png": "original.png",
    "image/heic": "original.heic",
}
_STANDARD_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


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
@require_GET
def record_list(request):
    context = records_context(request.patient, request.GET)
    context["document_deleted"] = request.GET.get("deleted") == "1"
    result_count = context["page_obj"].paginator.count
    record_product_event(
        "archive_viewed",
        {"document_count_bucket": count_bucket(result_count)},
        account_id=request.user.pk,
    )
    if context["query"]:
        record_product_event(
            "search_submitted",
            {
                "query_length": len(context["query"]),
                "result_count_bucket": count_bucket(result_count),
            },
            account_id=request.user.pk,
        )
    return protect_sensitive_html(
        render(request, "documents/records.html", context)
    )


@patient_required
@require_GET
def document_summary(request, document_id):
    document = get_object_or_404(
        document_detail_queryset(request.patient),
        pk=document_id,
    )
    context = document_detail_context(document)
    context["feedback_received"] = request.GET.get("feedback") == "thanks"
    context["retry_started"] = request.GET.get("retry") == "started"
    context["retry_unavailable"] = request.GET.get("retry") == "unavailable"
    record_product_event(
        "document_opened",
        {
            "document_type": context["document_type_code"],
            "processing_status": document.status,
        },
        account_id=request.user.pk,
    )
    if request.GET.get("source") == "search":
        try:
            position = int(request.GET.get("position", ""))
        except (TypeError, ValueError):
            position = 0
        if 1 <= position <= 300:
            record_product_event(
                "search_result_opened",
                {"result_position": position, "document_type": context["document_type_code"]},
                account_id=request.user.pk,
            )
    return protect_sensitive_html(render(request, "documents/detail.html", context))


@patient_required
@require_POST
def document_feedback(request, document_id):
    with transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(
                pk=document_id,
                patient_id=request.patient.pk,
                deleted_at__isnull=True,
            )
            .first()
        )
        if document is None:
            raise Http404("Document not found")
        version = document.parsing_versions.filter(active=True).first()
        version_key = str(version.pk) if version is not None else "original"
        _feedback, created = InaccuracyFeedback.objects.get_or_create(
            idempotency_key=f"{document.pk}:{version_key}",
            defaults={
                "document": document,
                "parsing_version": version,
                "category": "DOCUMENT_RECOGNITION",
            },
        )
        if created:
            document_type = (
                document.parsing_versions.filter(active=True)
                .values_list("document_summary__document_type", flat=True)
                .first()
                or "UNKNOWN"
            )
            record_product_event(
                "inaccurate_feedback",
                {"document_type": document_type, "field_category": "document"},
                account_id=request.user.pk,
            )
            record_audit_event(
                request.user.pk,
                "inaccuracy_feedback_created",
                document.pk,
                "succeeded",
            )
    return redirect(f"{reverse('documents:document_summary', args=(document.pk,))}?feedback=thanks#document-actions")


@patient_required
@require_POST
def document_reprocess(request, document_id):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    try:
        queue_user_reprocessing(request.patient, document.pk, dispatch=safe_enqueue_processing)
        result = "started"
    except ReprocessingUnavailable:
        result = "unavailable"
    return redirect(f"{reverse('documents:document_summary', args=(document.pk,))}?retry={result}#document-actions")


@patient_required
@require_http_methods(["GET", "POST"])
def document_delete(request, document_id):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    if request.method == "GET":
        return protect_sensitive_html(
            render(
                request,
                "documents/delete_confirm.html",
                {"document": document, "current_section": "records"},
            )
        )
    if request.POST.get("confirmation") != "delete":
        return protect_sensitive_html(
            render(
                request,
                "documents/delete_confirm.html",
                {
                    "document": document,
                    "current_section": "records",
                    "confirmation_error": True,
                },
                status=400,
            )
        )
    try:
        request_document_deletion(
            request.patient,
            document.pk,
            dispatch=safe_enqueue_document_deletion,
        )
    except DeletionRequestUnavailable:
        raise Http404("Document not found") from None
    return redirect(f"{reverse('documents:records')}?deleted=1")


@patient_required
@require_GET
def indicator_trend(request, standard_code):
    if _STANDARD_CODE.fullmatch(standard_code) is None:
        raise Http404("Trend not found")
    trend = trend_view(request.patient, standard_code)
    if trend is None:
        raise Http404("Trend not found")
    point_count = sum(len(series.points) for series in trend.series)
    record_product_event(
        "trend_opened",
        {"point_count": min(point_count, 300)},
        account_id=request.user.pk,
    )
    return protect_sensitive_html(
        render(
            request,
            "documents/trend.html",
            {"trend": trend, "current_section": "records"},
        )
    )


def _page_number(value, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(number, maximum))


def _highlight_rect(polygon):
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        if len(xs) < 3 or any(value < 0 or value > 1 for value in xs + ys):
            return ""
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        if left >= right or top >= bottom:
            return ""
    except (TypeError, ValueError, IndexError, KeyError):
        return ""
    return ",".join(
        f"{value * 100:.4f}"
        for value in (left, top, right - left, bottom - top)
    )


@patient_required
@require_GET
def document_viewer(request, document_id):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    page_number = _page_number(request.GET.get("page"), document.page_count)
    highlight_rect = ""
    evidence_value = request.GET.get("evidence", "")
    if evidence_value:
        try:
            evidence = (
                SourceEvidence.objects.select_related("document_page")
                .filter(
                    pk=evidence_value,
                    parsing_version__active=True,
                    parsing_version__document=document,
                )
                .first()
            )
        except (ValidationError, ValueError):
            evidence = None
        if evidence is not None:
            page_number = evidence.document_page.page_number
            highlight_rect = _highlight_rect(evidence.polygon)
    source = "evidence" if evidence_value else request.GET.get("source", "viewer")
    if source not in {"detail", "viewer", "evidence", "other"}:
        source = "other"
    record_product_event(
        "original_opened",
        {"source": source, "page_count_bucket": count_bucket(document.page_count)},
        account_id=request.user.pk,
    )
    if evidence_value:
        record_product_event(
            "evidence_opened",
            {"located": bool(evidence is not None and highlight_rect)},
            account_id=request.user.pk,
        )
    page_one_url = reverse("documents:document_page_image", args=(document.pk, 1))
    context = {
        "document": document,
        "current_section": "records",
        "initial_page": page_number,
        "initial_page_url": reverse("documents:document_page_image", args=(document.pk, page_number)),
        "highlight_page": page_number if highlight_rect else 0,
        "highlight_rect": highlight_rect,
        "page_numbers": range(1, document.page_count + 1),
        "page_url_template": page_one_url.replace("/1/image/", "/{page}/image/"),
        "thumbnail_sheet_url": reverse("documents:document_thumbnail_sheet", args=(document.pk,)),
        "embed": request.GET.get("embed") == "1",
    }
    template = "documents/viewer_embed.html" if context["embed"] else "documents/viewer.html"
    return protect_sensitive_html(render(request, template, context), embeddable=context["embed"])


def _protect_page_image(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Referrer-Policy"] = "no-referrer"
    return response


@patient_required
@require_GET
def document_page_image(request, document_id, page_number):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    if not 1 <= page_number <= document.page_count:
        raise Http404("Page not found")
    try:
        source = get_object_store().open_private(document.original_object_key)
        with closing(source):
            payload = render_page(
                source,
                document.content_type,
                page_number,
                thumbnail=request.GET.get("thumbnail") == "1",
            )
    except (UploadDomainError, ImproperlyConfigured, OSError, PreviewUnavailable):
        logger.warning(
            "document_preview_failed",
            extra={"error_code": "preview_unavailable"},
        )
        return _protect_page_image(
            HttpResponse("原件暂时无法打开，请重试。", status=503, content_type="text/plain; charset=utf-8")
        )
    return _protect_page_image(HttpResponse(payload, content_type="image/png"))


@patient_required
@require_GET
def document_thumbnail_sheet(request, document_id):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    try:
        source = get_object_store().open_private(document.original_object_key)
        with closing(source):
            payload = render_thumbnail_sheet(source, document.content_type, document.page_count)
    except (UploadDomainError, ImproperlyConfigured, OSError, PreviewUnavailable):
        logger.warning(
            "document_thumbnail_failed",
            extra={"error_code": "preview_unavailable"},
        )
        return _protect_page_image(
            HttpResponse("缩略页暂时无法打开。", status=503, content_type="text/plain; charset=utf-8")
        )
    return _protect_page_image(HttpResponse(payload, content_type="image/png"))


def _protect_original_response(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@patient_required
@require_GET
def document_original(request, document_id):
    document = get_object_or_404(
        Document,
        pk=document_id,
        patient_id=request.patient.pk,
        deleted_at__isnull=True,
    )
    record_product_event(
        "original_opened",
        {"source": "other", "page_count_bucket": count_bucket(document.page_count)},
        account_id=request.user.pk,
    )
    try:
        source = get_object_store().open_private(document.original_object_key)
    except (UploadDomainError, ImproperlyConfigured, OSError) as error:
        if isinstance(error, ImproperlyConfigured):
            reason = "configuration"
        elif isinstance(error, ObjectNotFound):
            reason = "not_found"
        elif isinstance(error, UploadDomainError):
            reason = "invalid_reference"
        else:
            reason = "storage_unavailable"
        safe_record_metric("phr_original_open_failure_total", {"reason": reason})
        logger.warning(
            "original_open_failed",
            extra={"error_code": reason},
        )
        return _protect_original_response(
            HttpResponse(
                "原件暂时无法打开，请稍后重试。",
                status=503,
                content_type="text/plain; charset=utf-8",
            )
        )
    response = FileResponse(
        source,
        as_attachment=False,
        filename=_OPAQUE_ORIGINAL_FILENAMES[document.content_type],
        content_type=document.content_type,
    )
    return _protect_original_response(response)


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
        batch = UploadBatch.objects.create(patient=request.patient, file_count=len(candidates))
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


def _begin_upload(patient, batch_id, item_id):
    with transaction.atomic():
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
    with transaction.atomic():
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
        item = _begin_upload(request.patient, batch_id, item_id)
    except UploadResourceNotFound:
        raise Http404
    except UploadStateConflict as error:
        return _error(error.code, 409)

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
    UploadItem.objects.filter(pk=item.pk, batch__patient_id=request.patient.pk).update(display_filename=safe_name)

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
    return f'"{hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()}"'


@patient_required
@require_GET
def batch_status(request, batch_id):
    batch = UploadBatch.objects.filter(pk=batch_id, patient_id=request.patient.pk).first()
    if batch is None:
        raise Http404
    items = list(batch.items.select_related("document").order_by("ordinal", "pk"))
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
                }
                for item in items
            ],
        }
    )
    response["ETag"] = etag
    return response
