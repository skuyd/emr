from django.http import Http404
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from apps.patients.models import Patient
from apps.patients.access import accessible_patients, authorize_patient
from django.core.exceptions import PermissionDenied
from django.apps import apps


# Only audited, immutable resource identities may select a patient on a read.
# Explicit scope always wins; mutations never infer scope from these resources.
RESOURCE_PATIENT_ROUTES = {
    "cloud_imaging:document": ("documents.Document", "document_id", "patient_id"),
    "cloud_imaging:source": ("cloud_imaging.CloudImagingSource", "source_id", "patient_id"),
    **dict.fromkeys(("glucose:detail", "glucose:edit", "glucose:recheck"),
                   ("glucose.GlucoseRecord", "glucose_record_id", "patient_id")),
    "glucose:import_lab": ("labs.LabObservation", "observation_id", "parsing_version__document__patient_id"),
    "glucose:import_nursing": ("documents.Document", "document_id", "patient_id"),
    **dict.fromkeys(("self_records:detail", "self_records:edit"),
                   ("self_records.DailyRecord", "record_id", "patient_id")),
    **dict.fromkeys((
        "documents:document_summary", "documents:document_delete", "documents:document_permanent_delete",
        "documents:document_viewer", "documents:document_page_image", "documents:document_thumbnail_sheet",
        "documents:document_original", "facts:document", "facts:reports",
    ), ("documents.Document", "document_id", "patient_id")),
    **dict.fromkeys(("exports:preview", "exports:pdf", "exports:download"),
                    ("exports.ExportJob", "job_id", "patient_id")),
    **dict.fromkeys(("labs:observation", "labs:observation_source", "labs:observation_source_image"),
                    ("labs.LabObservation", "observation_id", "parsing_version__document__patient_id")),
    "facts:detail": ("facts.Fact", "fact_id", "document__patient_id"),
    "facts:report": ("facts.ClinicalReport", "report_id", "document__patient_id"),
    "treatments:event": ("treatments.TreatmentEvent", "event_id", "patient_id"),
    "treatments:regimen": ("treatments.TreatmentRegimen", "regimen_id", "patient_id"),
    **dict.fromkeys(("treatments:cycle", "treatments:split", "treatments:assign"),
                    ("treatments.TreatmentCycle", "cycle_id", "patient_id")),
    "documents:batch_status": ("documents.UploadBatch", "batch_id", "patient_id"),
    "notifications:open": ("notifications.TaskNotification", "notification_id", "patient_id"),
}


class PatientSelectionRequired(Exception):
    pass


def _require_active_user(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        raise Http404("No Patient matches the given query.")
    return user


def _resource_patient_id(request):
    match = getattr(request, "resolver_match", None)
    binding = RESOURCE_PATIENT_ROUTES.get(match.view_name) if match else None
    if request.method not in {"GET", "HEAD"} or binding is None:
        return None
    model, argument, patient_field = binding
    identity = apps.get_model(model).objects.filter(pk=match.kwargs[argument]).values_list(patient_field, flat=True).first()
    if identity is None:
        raise Http404("No Patient matches the given query.")
    return identity


def get_request_patient(request):
    user = _require_active_user(request)
    explicit = request.headers.get("X-Patient-ID") or (
        request.POST.get("patient_id") if request.method == "POST" else request.GET.get("patient")
    )
    selected = explicit or _resource_patient_id(request) or getattr(request, "session", {}).get("active_patient_id")
    if selected:
        try:
            return authorize_patient(selected, user).patient
        except (PermissionDenied, ValidationError, ValueError, TypeError):
            raise Http404("No Patient matches the given query.") from None
    patients = list(accessible_patients(user)[:2])
    if len(patients) == 1:
        return patients[0]
    if len(patients) > 1:
        raise PatientSelectionRequired
    raise Http404("No Patient matches the given query.")


def get_patient_object_or_404(queryset, request, id):
    user = _require_active_user(request)
    try:
        return get_object_or_404(queryset.filter(pk__in=accessible_patients(user).values("pk"), pk=id))
    except (TypeError, ValidationError, ValueError):
        raise Http404("No Patient matches the given query.") from None
