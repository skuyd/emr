import uuid

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods

from apps.core.decorators import patient_required
from apps.documents.models import Document
from apps.patients.access import Capability

from .clinical_forms import BoundaryReplacementForm, ClinicalRevisionForm, ManualClinicalFieldForm, ReportActionForm, ReportForm
from .clinical_readmodels import report_queryset, report_source_token, review_reports
from .clinical_schema import FIELDS
from .clinical_services import add_manual_clinical_field, create_manual_report, replace_report_boundary, request_clinical_extraction, revise_report
from .models import ClinicalExtraction
from .readmodels import effective_fact
from .revisions import FactConflict, revise_fact


def _error(exc):
    return ("；".join(exc.messages), 400) if isinstance(exc, ValidationError) else (str(exc), 409)


@patient_required
@require_http_methods(["GET", "POST"])
def document_reports(request, document_id):
    from .views import _render

    document = get_object_or_404(Document, pk=document_id, patient=request.patient, deleted_at__isnull=True)
    version = document.parsing_versions.filter(active=True).first()
    form = ReportForm(initial={"first_page": 1, "last_page": document.page_count,
                               "expected_lifecycle_revision": document.lifecycle_revision,
                               "expected_version_id": str(version.pk) if version else ""})
    error, status = "", 200
    if request.method == "POST":
        try:
            if request.POST.get("action") == "extract":
                request_clinical_extraction(request.patient, actor=request.user, document_id=document.pk,
                                            expected_version=request.POST.get("expected_version_id"))
                return redirect("facts:reports", document_id=document.pk)
            form = ReportForm(request.POST)
            if form.is_valid():
                values = form.cleaned_data
                report = create_manual_report(request.patient, actor=request.user, document_id=document.pk,
                                               title=values["title"], spans=[{"page_number": n} for n in range(values["first_page"], values["last_page"] + 1)],
                                               expected_lifecycle_revision=values["expected_lifecycle_revision"],
                                               expected_version_id=values["expected_version_id"] or None)
                return redirect("facts:report", report_id=report.pk)
            status = 400
        except (ValidationError, FactConflict) as exc:
            error, status = _error(exc)
    return _render(request, "facts/reports.html", {
        "document": document, "reports": review_reports(request.patient, actor=request.user, document_id=document.pk, include_history=True),
        "form": form, "version": version, "error": error, "can_write": request.patient_access.permits(Capability.WRITE),
        "extraction": ClinicalExtraction.objects.filter(parsing_version=version).first() if version else None,
    }, status=status)


@patient_required
@require_http_methods(["GET", "POST"])
def report_detail(request, report_id):
    from .views import _render

    report = get_object_or_404(report_queryset(), pk=report_id, document__patient=request.patient, document__deleted_at__isnull=True)
    rows = review_reports(request.patient, actor=request.user, report_ids=[report.pk], include_history=True)
    if not rows:
        raise Http404
    row = rows[0]
    key = request.POST.get("field_key") or request.GET.get("field_key", "report.exam_date")
    if key not in FIELDS:
        raise Http404
    entities = []
    seen = set()
    for field in row["fields"]:
        if field["entity_key"] != "report" and field["entity_key"] not in seen:
            seen.add(field["entity_key"])
            label = next((f["content"]["value"]["text"] for f in row["fields"]
                          if f["entity_key"] == field["entity_key"] and f["field_key"] == "lesion.site"), "位置待核对")
            entities.append((field["entity_key"], f"病灶{len(entities)+1}：{label}"))
    form = ManualClinicalFieldForm(key, entities=entities, initial={"expected_report_source": row["current_source_token"], "page_number": row["pages"][0]})
    action_form = ReportActionForm(initial={"expected_revision": report.revision_number, "expected_source": row["current_source_token"]})
    boundary_form = BoundaryReplacementForm(report, initial={"title": report.title, "mode": "pages", "first_page": row["pages"][0], "last_page": row["pages"][-1],
                                                             "expected_revision": report.revision_number, "expected_source": row["current_source_token"]})
    error, status = "", 200
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "REPLACE":
                boundary_form = BoundaryReplacementForm(report, request.POST)
                if boundary_form.is_valid():
                    values = boundary_form.cleaned_data
                    replacement = replace_report_boundary(request.patient, actor=request.user, report_id=report.pk,
                                                          title=values["title"], spans=values["spans"], expected_revision=values["expected_revision"], expected_source=values["expected_source"])
                    return redirect("facts:report", report_id=replacement.pk)
            elif action in {"EXCLUDE", "UNDO"}:
                action_form = ReportActionForm(request.POST)
                if action_form.is_valid():
                    revise_report(request.patient, actor=request.user, report_id=report.pk, action=action, **action_form.cleaned_data)
                    return redirect("facts:report", report_id=report.pk)
            else:
                form = ManualClinicalFieldForm(key, request.POST, entities=entities)
                if form.is_valid():
                    values = form.cleaned_data
                    entity = "report" if FIELDS[key].entity_kind == "report" else ("lesion:" + uuid.uuid4().hex if values["entity"] == "new" else values["entity"])
                    fact = add_manual_clinical_field(request.patient, actor=request.user, report_id=report.pk,
                                                    entity_key=entity, field_key=key, value=values["value"],
                                                    fragments=[{"page_number": values["page_number"], "raw_text": values["raw_value"]}],
                                                    expected_report_source=values["expected_report_source"])
                    return redirect("facts:detail", fact_id=fact.pk)
            status = 400
        except (ValidationError, FactConflict) as exc:
            error, status = _error(exc)
    return _render(request, "facts/report.html", {"report": report, "row": row, "form": form, "action_form": action_form,
                                                  "boundary_form": boundary_form,
                                                  "field_keys": [(k, spec.label) for k, spec in FIELDS.items()], "field_key": key,
                                                  "error": error, "can_write": request.patient_access.permits(Capability.WRITE),
                                                  "history": report.revisions.select_related("author").order_by("-sequence")}, status=status)


def field_detail(request, fact):
    from .views import _render

    row = effective_fact(fact)
    initial = {"raw_value": row["content"]["raw_value"], "expected_revision": fact.revision_number, "expected_source": row["current_source_token"]}
    form = ClinicalRevisionForm(fact.field_key, value=row["content"]["value"], initial=initial)
    error, status = "", 200
    if request.method == "POST":
        form = ClinicalRevisionForm(fact.field_key, request.POST, value=row["content"]["value"])
        if form.is_valid():
            values, action = form.cleaned_data, request.POST.get("action", "")
            try:
                if action == "CONFIRM" and (values["value"] != row["content"]["value"] or values["raw_value"] != row["content"]["raw_value"]):
                    raise ValidationError("字段已有修改，请使用“保存更正并确认”。")
                revise_fact(request.patient, fact.pk, actor=request.user, action=action,
                            expected_revision=values["expected_revision"], expected_source=values["expected_source"],
                            checked_original=values["checked_original"],
                            changes={"value": values["value"], "raw_value": values["raw_value"]} if action == "CORRECT" else None)
                return redirect("facts:detail", fact_id=fact.pk)
            except (ValidationError, FactConflict) as exc:
                error, status = _error(exc)
        else:
            status = 400
    return _render(request, "facts/field.html", {"fact": fact, "row": row, "form": form, "error": error,
                                                 "can_write": request.patient_access.permits(Capability.WRITE),
                                                 "history": fact.revisions.select_related("author").order_by("-sequence")}, status=status)
