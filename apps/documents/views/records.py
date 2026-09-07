import re

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.analytics.events import count_bucket, record_product_event
from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.labs.trends import trend_summaries, trend_view
from apps.operations.audit import record_audit_event
from apps.processing.reprocessing import ReprocessingUnavailable, queue_user_reprocessing
from apps.processing.material_review import MaterialReviewConflict, review_material
from apps.processing.tasks import safe_enqueue_processing

from ..archive import records_context
from ..detail import document_detail_context, document_detail_queryset
from ..lifecycle import LifecycleUnavailable, move_to_trash
from ..models import Document, InaccuracyFeedback


_STANDARD_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


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
    from apps.patients.access import authorize_patient
    response = render(request, "documents/records.html", context)
    authorize_patient(request.patient, request.user)
    return protect_sensitive_html(response)


@patient_required
@require_GET
def document_summary(request, document_id):
    document = get_object_or_404(
        document_detail_queryset(request.patient),
        pk=document_id,
    )
    context = document_detail_context(document)
    from apps.facts.clinical_readmodels import review_reports
    context["clinical_reports"] = review_reports(request.patient, actor=request.user, document_id=document.pk)
    context["feedback_received"] = request.GET.get("feedback") == "thanks"
    context["retry_started"] = request.GET.get("retry") == "started"
    context["retry_unavailable"] = request.GET.get("retry") == "unavailable"
    context["material_saved"] = request.GET.get("material") in {"kept", "auto"}
    context["material_can_write"] = request.patient_access.permits("write")
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
    from apps.patients.access import authorize_patient
    response = render(request, "documents/detail.html", context)
    authorize_patient(request.patient, request.user)
    if not Document.objects.filter(pk=document.pk, patient=request.patient, deleted_at__isnull=True).exists():
        raise Http404("Document not found")
    return protect_sensitive_html(response)


@patient_required
@require_POST
def document_material(request, document_id):
    document = get_object_or_404(document_detail_queryset(request.patient), pk=document_id)
    try:
        try:
            revision = int(request.POST.get("expected_revision", ""))
        except (TypeError, ValueError):
            raise MaterialReviewConflict("保留方式已变化，请刷新后重试。") from None
        review_material(
            request.patient, document.pk, actor=request.user,
            action=request.POST.get("action", ""),
            expected_version=request.POST.get("expected_version", ""),
            expected_revision=revision, dispatch=safe_enqueue_processing,
        )
    except MaterialReviewConflict as error:
        context = document_detail_context(document)
        context["material_error"] = str(error)
        context["material_can_write"] = request.patient_access.permits("write")
        return protect_sensitive_html(render(request, "documents/detail.html", context, status=409))
    result = "kept" if request.POST.get("action") == "KEEP_DOCUMENT" else "auto"
    return redirect(f"{reverse('documents:document_summary', args=(document.pk,))}?material={result}#material-review")


@patient_required
@require_POST
def document_feedback(request, document_id):
    with transaction.atomic():
        from apps.patients.access import authorize_patient
        authorize_patient(request.patient, request.user, "write", lock=True)
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
        queue_user_reprocessing(request.patient, document.pk, actor=request.user, dispatch=safe_enqueue_processing)
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
        move_to_trash(
            request.patient,
            document.pk,
            actor=request.user,
        )
    except LifecycleUnavailable:
        raise Http404("Document not found") from None
    return redirect(f"{reverse('documents:records')}?deleted=1")


@patient_required
@require_GET
def trend_index(request):
    return protect_sensitive_html(
        render(
            request,
            "documents/trends.html",
            {"trends": trend_summaries(request.patient), "current_section": "trends"},
        )
    )


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
            {"trend": trend, "current_section": "trends"},
        )
    )
