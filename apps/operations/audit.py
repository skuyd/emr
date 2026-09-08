import hashlib
import hmac
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.apps import apps
from django.conf import settings

from .models import AuditEvent


ALLOWED_ACTIONS = frozenset(
    {
        "inaccuracy_feedback_created",
        "product_feedback_created",
        "patient_name_changed",
        "patient_created",
        "patient_deletion_requested",
        "member_role_changed",
        "member_access_revoked",
        "notification_preference_changed",
        "push_subscription_created",
        "push_subscription_revoked",
        "document_deletion_requested",
        "document_trashed",
        "document_restored",
        "document_material_reviewed",
        "fact_added",
        "fact_revised",
        "clinical_extraction_requested",
        "clinical_report_added",
        "clinical_report_revised",
        "clinical_field_added",
        "laterality_scope_changed",
        "laterality_scope_viewed",
        "clinical_report_viewed",
        "document_deletion_purged",
        "account_deletion_requested",
        "account_deletion_purged",
        "processing_requeued",
        "parsing_version_activated",
        "dictionary_published",
        "quota_changed",
        "support_access_granted",
        "support_access_used",
        "deletion_status_viewed",
        "patient_viewed", "document_viewed", "source_viewed", "fact_viewed", "lab_viewed",
        "export_viewed", "original_downloaded", "export_downloaded", "audit_viewed",
        "members_viewed", "invitation_viewed", "share_viewed", "notification_viewed",
        "review_viewed", "access_attempted", "document_uploaded", "upload_started", "upload_removed",
        "self_record_created", "self_record_revised", "self_record_viewed",
        "lesion_created", "lesion_renamed", "lesion_relations_changed", "lesion_viewed",
        "glucose_record_created", "glucose_record_revised", "glucose_record_viewed",
        "lab_revised", "export_preview_created", "export_requested", "export_generated", "export_cancelled",
        "invitation_created", "invitation_accepted", "invitation_revoked",
        "share_created", "share_revoked", "share_access_granted", "share_expired", "share_invalidated",
        "treatment_event_created", "treatment_event_revised",
        "treatment_cycle_created", "treatment_cycle_revised",
        "treatment_regimen_created", "treatment_regimen_revised",
        "treatment_derivation_created",
        "treatments_viewed", "treatment_event_viewed", "treatment_regimen_viewed", "treatment_cycle_viewed",
    }
)
ALLOWED_RESULTS = frozenset({"succeeded", "denied", "failed", "scheduled"})
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ROUTE = re.compile(r"[a-z][a-z0-9_:.-]{0,99}")
RESOURCE_TYPES = frozenset({"patient", "document", "upload_batch", "upload_item", "fact", "lab_observation",
                            "parsing_version", "member", "invitation", "share", "export", "notification",
                            "review", "support", "quota", "dictionary", "feedback", "account", "system",
                            "clinical_report", "treatment_event", "treatment_regimen", "treatment_cycle", "self_record", "glucose_record",
                            "lesion", "lesion_operation", "lesion_proposal", "laterality_operation"})


@dataclass
class AuditRequest:
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    route_name: str = ""
    emitted: int = 0


current_audit_request = ContextVar("current_audit_request", default=None)


# Old service calls retain their signatures. Resolve only opaque identities;
# this lookup never grants access and never reads medical fields.
ACTION_SUBJECTS = {
    "laterality_scope_changed": ("laterality_operation", "facts.LateralityScopeOperation", "patient_id"),
    "laterality_scope_viewed": ("laterality_operation", "facts.LateralityScopeOperation", "patient_id"),
    **dict.fromkeys(("lesion_created", "lesion_renamed"), ("lesion", "lesions.Lesion", "patient_id")),
    "lesion_relations_changed": ("lesion_operation", "lesions.LesionOperation", "patient_id"),
    **dict.fromkeys(("glucose_record_created", "glucose_record_revised", "glucose_record_viewed"),
                    ("glucose_record", "glucose.GlucoseRecord", "patient_id")),
    "treatment_derivation_created": ("patient", "patients.Patient", "pk"),
    **dict.fromkeys(("treatment_event_created", "treatment_event_revised"),
                    ("treatment_event", "treatments.TreatmentEvent", "patient_id")),
    **dict.fromkeys(("treatment_cycle_created", "treatment_cycle_revised"),
                    ("treatment_cycle", "treatments.TreatmentCycle", "patient_id")),
    **dict.fromkeys(("treatment_regimen_created", "treatment_regimen_revised"),
                    ("treatment_regimen", "treatments.TreatmentRegimen", "patient_id")),
    **dict.fromkeys(("self_record_created", "self_record_revised", "self_record_viewed"),
                    ("self_record", "self_records.DailyRecord", "patient_id")),
    **dict.fromkeys(("patient_created", "patient_name_changed", "patient_deletion_requested", "notification_preference_changed",
                     "push_subscription_revoked"), ("patient", "patients.Patient", "pk")),
    "quota_changed": ("quota", "patients.Patient", "pk"),
    **dict.fromkeys(("document_uploaded", "document_deletion_requested", "document_trashed", "document_restored",
                     "document_deletion_purged", "processing_requeued", "document_material_reviewed"), ("document", "documents.Document", "patient_id")),
    **dict.fromkeys(("fact_added", "fact_revised"), ("fact", "facts.Fact", "document__patient_id")),
    "clinical_extraction_requested": ("document", "documents.Document", "patient_id"),
    **dict.fromkeys(("clinical_report_added", "clinical_report_revised", "clinical_report_viewed"),
                    ("clinical_report", "facts.ClinicalReport", "document__patient_id")),
    "clinical_field_added": ("fact", "facts.Fact", "document__patient_id"),
    "lab_revised": ("lab_observation", "labs.LabObservation", "parsing_version__document__patient_id"),
    "parsing_version_activated": ("parsing_version", "processing.ParsingVersion", "document__patient_id"),
    **dict.fromkeys(("member_role_changed", "member_access_revoked"), ("member", "patients.PatientMembership", "patient_id")),
    **dict.fromkeys(("invitation_created", "invitation_accepted", "invitation_revoked"), ("invitation", "patients.PatientInvitation", "patient_id")),
    **dict.fromkeys(("share_created", "share_revoked", "share_access_granted", "share_expired", "share_invalidated"),
                    ("share", "patients.PatientShare", "patient_id")),
    **dict.fromkeys(("export_preview_created", "export_requested", "export_generated", "export_cancelled"),
                    ("export", "exports.ExportJob", "patient_id")),
    "product_feedback_created": ("feedback", "patients.ProductFeedback", "patient_id"),
    "inaccuracy_feedback_created": ("feedback", "documents.InaccuracyFeedback", "document__patient_id"),
    "push_subscription_created": ("notification", "notifications.PushSubscription", "patient_id"),
    **dict.fromkeys(("support_access_granted", "support_access_used"), ("support", "operations.SupportAccessGrant", "patient_id")),
}


class InvalidAuditEvent(ValueError):
    pass


def _hash(domain, value):
    if isinstance(value, str) and value in {"system", "anonymous"}:
        canonical = value
    else:
        try:
            canonical = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise InvalidAuditEvent(f"{domain} must be an opaque UUID") from None
    key = settings.AUDIT_HASH_KEY.encode("utf-8")
    return hmac.new(key, f"phr-audit/{domain}/v1:{canonical}".encode("ascii"), hashlib.sha256).hexdigest()


def record_audit_event(actor, action, target_id, result, reason=None, *, patient_id=None,
                       resource_type=None, route_name=None, request_id=None):
    if action not in ALLOWED_ACTIONS or result not in ALLOWED_RESULTS:
        raise InvalidAuditEvent("Unknown audit action or result")
    if reason is not None and _REASON.fullmatch(reason) is None:
        raise InvalidAuditEvent("Audit reasons must be stable non-sensitive codes")
    subject = ACTION_SUBJECTS.get(action)
    if subject:
        inferred_type, model, patient_field = subject
        resource_type = resource_type or inferred_type
        if patient_id is None:
            patient_id = apps.get_model(model).objects.filter(pk=target_id).values_list(patient_field, flat=True).first()
    state = current_audit_request.get()
    route_name = (state.route_name if state else "") if route_name is None else route_name
    request_id = request_id or (state.request_id if state else None)
    if resource_type is not None and resource_type not in RESOURCE_TYPES:
        raise InvalidAuditEvent("Unknown audit resource type")
    if route_name and _ROUTE.fullmatch(route_name) is None:
        raise InvalidAuditEvent("Audit routes must be stable names")
    actor = getattr(actor, "pk", actor)
    event = AuditEvent.objects.create(
        actor_hash=_hash("actor", actor),
        action=action,
        target_hash=_hash("target", target_id),
        result=result,
        reason_code=reason or "",
        patient_hash=_hash("patient", patient_id) if patient_id else "",
        resource_type=resource_type or "",
        actor_kind=actor if actor in {"system", "anonymous"} else "account",
        route_name=route_name,
        request_id=request_id,
    )
    if state:
        state.emitted += 1
    return event
