from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.documents.models import Document

from .extraction import extract_version_facts
from .forms import FactRevisionForm, ManualFactForm
from .models import FactExtraction
from .readmodels import effective_fact, fact_queryset, review_facts
from .read_guards import source_read
from .revisions import FactConflict, _lock_document, add_manual_fact, revise_fact


def assert_render_access(request, context):
    from apps.patients.access import authorize_patient
    authorize_patient(request.patient, request.user)
    source = context.get("document") or getattr(context.get("fact"), "document", None) or getattr(context.get("report"), "document", None)
    if source is not None and not Document.objects.filter(pk=source.pk, patient=request.patient, deleted_at__isnull=True).exists():
        raise Http404("Source not found")


def _render(request, template, context, status=200):
    response = render(request, template, {"current_section": "records", **context}, status=status)
    assert_render_access(request, context)
    return protect_sensitive_html(response)


@patient_required
@require_GET
@source_read
def fact_index(request):
    return _render(request, "facts/index.html", {
        "rows": review_facts(request.patient, include_history=True),
        "documents": Document.objects.filter(patient=request.patient, deleted_at__isnull=True).order_by("-created_at"),
    })


@patient_required
@require_http_methods(["GET", "POST"])
@source_read
def document_facts(request, document_id):
    document = get_object_or_404(Document, pk=document_id, patient=request.patient, deleted_at__isnull=True)
    form = ManualFactForm(initial={"page_number": 1})
    error, status = "", 200
    if request.method == "POST":
        try:
            if request.POST.get("action") == "extract":
                with transaction.atomic():
                    locked = _lock_document(request.patient, document_id, actor=request.user)
                    version = locked.parsing_versions.filter(active=True).first()
                    if version is None:
                        raise ValidationError("当前没有可用 OCR，请查看原件并人工补录。")
                    failed = FactExtraction.objects.filter(parsing_version=version, status="FAILED")
                    failed.delete()
                    extract_version_facts(version)
                return redirect("facts:document", document_id=document_id)
            form = ManualFactForm(request.POST)
            if form.is_valid():
                fact = add_manual_fact(request.patient, document_id, actor=request.user, **form.cleaned_data)
                return redirect("facts:detail", fact_id=fact.pk)
            status = 400
        except (ValidationError, FactConflict) as exc:
            error = "；".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            status = 409 if isinstance(exc, FactConflict) else 400
        except PermissionDenied:
            raise Http404("Source not found") from None
    version = document.parsing_versions.filter(active=True).first()
    return _render(request, "facts/document.html", {
        "document": document, "rows": review_facts(request.patient, document=document, include_history=True),
        "form": form, "error": error, "version": version,
        "extraction": FactExtraction.objects.filter(parsing_version=version).first() if version else None,
    }, status=status)


@patient_required
@require_http_methods(["GET", "POST"])
@source_read
def fact_detail(request, fact_id):
    fact = get_object_or_404(fact_queryset(), pk=fact_id, document__patient=request.patient, document__deleted_at__isnull=True)
    if fact.representation == "FIELD":
        from .clinical_views import field_detail
        return field_detail(request, fact)
    row = effective_fact(fact)
    form = FactRevisionForm(initial={
        **row["content"], "record_date_raw": (row["content"].get("record_date") or {}).get("raw", ""),
        "expected_revision": row["revision_number"], "expected_source": row["current_source_token"],
    })
    error, status = "", 200
    if request.method == "POST":
        form = FactRevisionForm(request.POST)
        if form.is_valid():
            values = form.cleaned_data
            action = request.POST.get("action", "")
            changes = {key: values[key] for key in ("category", "text", "date_raw", "record_date_raw", "institution")} if action == "CORRECT" else None
            try:
                if action == "CONFIRM" and any(
                    values[key] != (
                        (row["content"].get("record_date") or {}).get("raw", "") if key == "record_date_raw"
                        else row["content"].get(key, "")
                    ) for key in ("category", "text", "date_raw", "record_date_raw", "institution")
                ):
                    raise ValidationError("表单内容已有修改，请使用“保存更正并确认”。")
                revise_fact(request.patient, fact_id, actor=request.user, action=action, changes=changes,
                            expected_revision=values["expected_revision"], checked_original=values["checked_original"],
                            expected_source=values["expected_source"])
                return redirect("facts:detail", fact_id=fact_id)
            except (ValidationError, FactConflict) as exc:
                error = "；".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                status = 409 if isinstance(exc, FactConflict) else 400
            except PermissionDenied:
                raise Http404("Source not found") from None
        else:
            status = 400
    return _render(request, "facts/detail.html", {
        "fact": fact, "row": row, "form": form, "error": error,
        "history": fact.revisions.order_by("-sequence").select_related("author"),
    }, status=status)
