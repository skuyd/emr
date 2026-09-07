import logging
from contextlib import closing

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.analytics.events import count_bucket, record_product_event
from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.core.streams import GuardedStream, guarded_file_response
from apps.patients.access import authorize_patient
from django.core.exceptions import PermissionDenied
from apps.operations.metrics import safe_record_metric
from apps.processing.models import DocumentType, SourceEvidence

from ..backends import get_object_store
from ..errors import ObjectNotFound, UploadDomainError
from ..models import Document
from ..previews import PreviewUnavailable, render_page, render_thumbnail_sheet
from ..titles import document_title, with_title_evidence


logger = logging.getLogger(__name__)
_OPAQUE_ORIGINAL_FILENAMES = {
    "application/pdf": "original.pdf",
    "image/jpeg": "original.jpg",
    "image/png": "original.png",
    "image/heic": "original.heic",
}


def _check_original_access(request, document):
    authorize_patient(document.patient_id, request.user)
    if not Document.objects.filter(pk=document.pk, patient_id=document.patient_id, deleted_at__isnull=True).exists():
        raise PermissionDenied


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
    if source == "search":
        try:
            position = int(request.GET.get("position", ""))
        except (TypeError, ValueError):
            position = 0
        if 1 <= position <= 300:
            document_type = (
                document.parsing_versions.filter(active=True)
                .values_list("document_summary__document_type", flat=True)
                .first()
                or DocumentType.UNKNOWN
            )
            if document_type not in DocumentType.values:
                document_type = DocumentType.UNKNOWN
            record_product_event(
                "search_result_opened",
                {"result_position": position, "document_type": document_type},
                account_id=request.user.pk,
            )
        # `original_opened.source` has a closed schema; retain the canonical
        # viewer origin while recording the search-result event above.
        source = "viewer"
    elif source not in {"detail", "viewer", "evidence", "other"}:
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
    version = with_title_evidence(
        document.parsing_versions.filter(active=True).select_related("document_summary")
    ).first()
    context = {
        "document": document,
        "document_title": document_title(document, version),
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
    _check_original_access(request, document)
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
    _check_original_access(request, document)
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
    try:
        _check_original_access(request, document)
    except PermissionDenied:
        source.close()
        raise
    response = FileResponse(
        GuardedStream(source, lambda: _check_original_access(request, document)),
        as_attachment=True,
        filename=_OPAQUE_ORIGINAL_FILENAMES[document.content_type],
        content_type=document.content_type,
    )
    return _protect_original_response(guarded_file_response(response))
