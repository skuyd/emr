"""Audit actual resource requests; these lookups never confer authorization."""

from dataclasses import dataclass
import uuid

from django.apps import apps
from django.core.exceptions import ValidationError
from django.urls import Resolver404, resolve
from django.views.decorators.debug import sensitive_variables

from .audit import AuditRequest, current_audit_request, record_audit_event


@dataclass(frozen=True)
class Subject:
    patient_id: object
    target_id: object
    resource_type: str


IDENTITIES = (
    ("document_id", "documents.Document", "patient_id", "document"),
    ("fact_id", "facts.Fact", "document__patient_id", "fact"),
    ("report_id", "facts.ClinicalReport", "document__patient_id", "clinical_report"),
    ("observation_id", "labs.LabObservation", "parsing_version__document__patient_id", "lab_observation"),
    ("job_id", "exports.ExportJob", "patient_id", "export"),
    ("item_id", "documents.UploadItem", "batch__patient_id", "upload_item"),
    ("batch_id", "documents.UploadBatch", "patient_id", "upload_batch"),
    ("notification_id", "notifications.TaskNotification", "patient_id", "notification"),
    ("task_id", "labs.ReviewTask", "observation__parsing_version__document__patient_id", "review"),
    ("version_id", "processing.ParsingVersion", "document__patient_id", "parsing_version"),
    ("invitation_id", "patients.PatientInvitation", "patient_id", "invitation"),
    ("share_id", "patients.PatientShare", "patient_id", "share"),
    ("patient_id", "patients.Patient", "pk", "patient"),
)
PATIENT_NAMESPACES = {"documents", "facts", "exports", "labs", "patients_family", "patient_profile", "notifications", "shared", "family_invitation"}
PATIENT_ROUTES = {"home", "profile", "update_profile_name", "submit_product_feedback", "update_notification_preference"}
READ_ACTIONS = {
    "document": "document_viewed", "fact": "fact_viewed", "lab_observation": "lab_viewed",
    "export": "export_viewed", "notification": "notification_viewed", "review": "review_viewed",
    "share": "share_viewed", "invitation": "invitation_viewed",
    "clinical_report": "clinical_report_viewed",
}
SOURCE_NAMES = {"document_viewer", "document_page_image", "document_thumbnail_sheet", "observation_source",
                "observation_source_image", "review_source", "review_source_image", "document", "page_image", "thumbnails"}
DOWNLOAD_NAMES = {"documents:document_original", "shared:original", "exports:download", "exports:pdf"}
QUIET_POLL_ROUTES = {"shared:status", "documents:batch_status", "notifications:list"}
SERVICE_AUDITED_ROUTES = {"labs:reviews"}
PUBLIC_LANDINGS = {"shared:open", "family_invitation:landing", "notifications:service_worker"}
MUTATION_ACTIONS = {
    "family_invitation:inspect": "invitation_viewed", "family_invitation:accept": "invitation_accepted",
    "shared:exchange": "share_access_granted", "patients_family:invitations": "invitation_created",
    "patients_family:revoke_invitation": "invitation_revoked", "patients_family:shares": "share_created",
    "patients_family:revoke_share": "share_revoked", "patients_family:members": "member_role_changed",
    "patients_family:delete": "patient_deletion_requested", "patients_family:create": "patient_created",
    "patient_profile:update_name": "patient_name_changed", "documents:create_batch": "upload_started",
    "documents:upload_item_content": "document_uploaded", "documents:remove_upload_item": "upload_removed",
    "documents:document_delete": "document_trashed", "documents:document_restore": "document_restored",
    "documents:document_permanent_delete": "document_deletion_requested", "documents:document_reprocess": "processing_requeued",
    "documents:document_material": "document_material_reviewed",
    "facts:detail": "fact_revised", "facts:document": "fact_added", "labs:observation": "lab_revised",
    "facts:report": "clinical_report_revised", "facts:reports": "access_attempted",
    "labs:activate_version": "parsing_version_activated", "exports:prepare": "export_preview_created",
    "exports:preview": "export_requested", "exports:cancel": "export_cancelled",
}


def _lookup(model, identity, field):
    try:
        return apps.get_model(model).objects.filter(pk=identity).values_list(field, flat=True).first()
    except (ValueError, TypeError, ValidationError):
        return None


@sensitive_variables()
def _token_subject(request, route):
    from apps.patients.tokens import token_digest

    if route in {"family_invitation:inspect", "family_invitation:accept"}:
        purpose, model, kind = "invitation", "patients.PatientInvitation", "invitation"
    elif route == "shared:exchange":
        purpose, model, kind = "share", "patients.PatientShare", "share"
    else:
        return None
    digest = token_digest(request.POST.get("token", ""), purpose)
    row = apps.get_model(model).objects.filter(token_digest=digest).values("id", "patient_id").first() if digest else None
    return Subject(row["patient_id"], row["id"], kind) if row else Subject(None, uuid.UUID(int=0), kind)


def resolve_subject(request, match):
    if not match or (match.namespace not in PATIENT_NAMESPACES and match.view_name not in PATIENT_ROUTES):
        return None
    if match.view_name in PUBLIC_LANDINGS or (match.namespace == "labs" and match.url_name.startswith(("dictionary", "second_factor"))):
        return None
    token_subject = _token_subject(request, match.view_name) if request.method == "POST" else None
    if token_subject:
        return token_subject
    # Shared URLs are associated with the grant's patient. A forged second
    # document ID must not redirect an audit into another patient's history.
    if match.namespace == "shared" and "share_id" in match.kwargs:
        share_id = match.kwargs["share_id"]
        patient_id = _lookup("patients.PatientShare", share_id, "patient_id")
        if patient_id:
            document_id = match.kwargs.get("document_id")
            return Subject(patient_id, document_id or share_id, "document" if document_id else "share")
    for argument, model, field, kind in IDENTITIES:
        if argument in match.kwargs:
            identity = match.kwargs[argument]
            return Subject(_lookup(model, identity, field), identity, kind)
    patient = getattr(request, "patient", None)
    if patient is not None:
        return Subject(patient.pk, patient.pk, "patient")
    selected = request.headers.get("X-Patient-ID") or (
        request.POST.get("patient_id") if request.method == "POST" else request.GET.get("patient")
    ) or getattr(request, "session", {}).get("active_patient_id")
    patient_id = _lookup("patients.Patient", selected, "pk") if selected else None
    return Subject(patient_id, patient_id, "patient") if patient_id else None


def _action(route, subject, method):
    if route in DOWNLOAD_NAMES:
        return "export_downloaded" if route.startswith("exports:") else "original_downloaded"
    if method not in {"GET", "HEAD"}:
        return MUTATION_ACTIONS.get(route, "access_attempted")
    if route == "patients_family:audit":
        return "audit_viewed"
    if route == "patients_family:members":
        return "members_viewed"
    if route == "patients_family:invitations":
        return "invitation_viewed"
    if route == "patients_family:shares":
        return "share_viewed"
    if route.split(":")[-1] in SOURCE_NAMES and (subject.resource_type != "fact"):
        return "source_viewed"
    return READ_ACTIONS.get(subject.resource_type, "patient_viewed")


class PatientAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        state = AuditRequest(route_name=match.view_name)
        token = current_audit_request.set(state)
        actor = request.user.pk if request.user.is_authenticated else "anonymous"
        try:
            # Resolve before mutation, so deletion cannot erase audit linkage.
            subject = resolve_subject(request, match)
            response = self.get_response(request)
            if match.view_name in SERVICE_AUDITED_ROUTES:
                # Cross-patient review queues audit each actual task in the
                # service. Never attribute their body to a session selection.
                return response
            subject = subject or resolve_subject(request, match)
            if subject is None:
                return response
            action = _action(match.view_name, subject, request.method)
            fields = {"patient_id": subject.patient_id, "resource_type": subject.resource_type,
                      "route_name": state.route_name, "request_id": state.request_id}
            emit = lambda result, reason=None: record_audit_event(actor, action, subject.target_id, result, reason, **fields)
            status = response.status_code
            authenticated = request.user.is_authenticated and request.user.is_active
            consent_redirect = status in {301, 302, 303, 307, 308} and response.get("Location", "").startswith(("/login/", "/onboarding/"))
            if status >= 400 or not authenticated or consent_redirect:
                emit("denied" if status in {401, 403, 404, 410} or not authenticated or consent_redirect else "failed", "http_denied" if status < 500 else "server_error")
            elif match.view_name in QUIET_POLL_ROUTES:
                pass
            elif response.streaming and match.view_name in DOWNLOAD_NAMES:
                emit("scheduled")
                source = response.streaming_content
                guard = getattr(response, "_guarded_stream", None)
                completed = finalized = False

                def finalize_download():
                    nonlocal finalized
                    if finalized:
                        return
                    finalized = True
                    if guard is not None and guard.denied:
                        emit("denied", "access_changed")
                    elif completed and (guard is None or guard.exhausted):
                        emit("succeeded")
                    else:
                        emit("failed", "stream_interrupted")

                def audited_stream():
                    nonlocal completed
                    try:
                        yield from source
                        completed = True
                    finally:
                        finalize_download()

                # WSGI may close the response before starting the generator.
                # Its finally block then never runs, so response shutdown must
                # share the same idempotent completion callback.
                response._resource_closers.append(finalize_download)
                response.streaming_content = audited_stream()
            elif request.method in {"GET", "HEAD"} or not state.emitted:
                emit("succeeded")
            return response
        finally:
            current_audit_request.reset(token)
