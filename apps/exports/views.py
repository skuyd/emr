from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.documents.backends import get_object_store

from .errors import ExportInputError, ExportUnavailable, PdfUnavailable
from .forms import GenerationForm, SelectionForm
from .models import ExportJob
from .pdf import card_sections, render_pdf, scope_text
from .selection import select_documents
from .services import cancel_export, create_preview, download_export, get_preview, request_generation
from .tasks import safe_enqueue_export


def _render(request, template, context, status=200):
    return protect_sensitive_html(render(request, template, {"current_section": "records", **context}, status=status))


def _file(artifact, *, inline=False):
    response = FileResponse(artifact.stream, content_type=artifact.content_type, as_attachment=not inline, filename=artifact.filename)
    response.block_size = 64 * 1024
    response["X-Content-Type-Options"] = "nosniff"
    return protect_sensitive_html(response, embeddable=inline)


@patient_required
@require_http_methods(["GET", "POST"])
def prepare(request):
    initial = {}
    if request.GET.get("edit"):
        try:
            identity = UUID(request.GET["edit"])
        except ValueError:
            raise Http404("Export not found") from None
        previous = get_object_or_404(ExportJob, pk=identity, patient=request.patient)
        initial = previous.snapshot.get("selection", {})
    form = SelectionForm(request.patient, request.POST if request.method == "POST" else None, initial=initial)
    manifest, error, status = None, "", 200
    if request.method == "POST":
        if form.is_valid():
            selection = form.selection()
            try:
                manifest = select_documents(request.patient, selection)
                if request.POST.get("action") == "preview":
                    job = create_preview(request.patient, request.session.session_key, selection)
                    return redirect("exports:preview", job_id=job.pk)
            except (ExportInputError, ExportUnavailable) as exc:
                error, status = str(exc), 400
            except PermissionDenied:
                raise Http404("Export sources not found") from None
        else:
            status = 400
    if manifest is not None and form.is_valid() and form.cleaned_data["mode"] == "dates":
        # Unknown and partially overlapping dates appear separately after filtering.
        form.fields["unknown_ids"].choices = [
            (row["id"], f'{row["filename"]} · {row["date_raw"] or "日期未知"} · {row["reason"]}')
            for row in manifest["uncertain"]
        ]
    else:
        form.fields["unknown_ids"].choices = []
    return _render(request, "exports/prepare.html", {
        "form": form, "manifest": manifest, "error": error,
        "jobs": ExportJob.objects.filter(patient=request.patient).order_by("-created_at")[:20],
    }, status)


@patient_required
@require_http_methods(["GET", "POST"])
def preview(request, job_id):
    owned = get_object_or_404(ExportJob, pk=job_id, patient=request.patient)
    error, status, pdf_error = "", 200, ""
    job = owned
    generation_form = GenerationForm(
        request.POST if request.method == "POST" else None,
        initial=job.options or {"format": "pdf", "parts": ["pdf", "originals", "json"]},
    )
    try:
        job = get_preview(request.patient, request.session.session_key, job_id)
        if request.method == "POST":
            if generation_form.is_valid():
                request_generation(request.patient, request.session.session_key, job_id, generation_form.cleaned_data,
                                   dispatch=safe_enqueue_export)
                return redirect("exports:preview", job_id=job_id)
            status = 400
    except (ExportInputError, ExportUnavailable) as exc:
        error, status = str(exc), 409
        job.refresh_from_db()
    except PermissionDenied:
        raise Http404("Export not found") from None
    sections = []
    if job.snapshot and not error:
        try:
            render_pdf(job.snapshot)
        except PdfUnavailable as exc:
            pdf_error = str(exc)
        sections = card_sections(job.snapshot)
    return _render(request, "exports/preview.html", {
        "job": job, "snapshot": job.snapshot if not error else {}, "card_sections": sections,
        "scope": scope_text(job.snapshot) if job.snapshot and not error else "",
        "error": error, "pdf_error": pdf_error, "form": generation_form,
    }, status)


@patient_required
@require_GET
def preview_pdf(request, job_id):
    from .formats import Artifact

    try:
        # Lock the same sources across rendering; also recheck time and session after it.
        unavailable = None
        with transaction.atomic():
            try:
                job = get_preview(request.patient, request.session.session_key, job_id)
                payload = render_pdf(job.snapshot)
                get_preview(request.patient, request.session.session_key, job_id)
            except ExportUnavailable as exc:
                # Commit the invalidation/scrubbing performed by get_preview.
                # Raising through this outer transaction would roll it back.
                unavailable = exc
        if unavailable:
            raise unavailable
        return _file(Artifact(payload, "visit-card-preview.pdf", "application/pdf"), inline=True)
    except PermissionDenied:
        raise Http404("Export not found") from None
    except (ExportUnavailable, PdfUnavailable) as exc:
        return _render(request, "exports/unavailable.html", {"error": str(exc)}, 409)


@patient_required
@require_GET
def download(request, job_id):
    try:
        artifact = download_export(request.patient, request.session.session_key, job_id, get_object_store())
    except PermissionDenied:
        raise Http404("Export not found") from None
    except ExportUnavailable as exc:
        return _render(request, "exports/unavailable.html", {"error": str(exc)}, 409)
    return _file(artifact)


@patient_required
@require_POST
def cancel(request, job_id):
    try:
        cancel_export(request.patient, request.session.session_key, job_id)
    except PermissionDenied:
        raise Http404("Export not found") from None
    return redirect("exports:prepare")
