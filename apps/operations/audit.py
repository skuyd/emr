import hashlib
import hmac
import re
import uuid

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
    }
)
ALLOWED_RESULTS = frozenset({"succeeded", "denied", "failed", "scheduled"})
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


class InvalidAuditEvent(ValueError):
    pass


def _hash(domain, value):
    if value == "system":
        canonical = value
    else:
        try:
            canonical = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise InvalidAuditEvent(f"{domain} must be an opaque UUID") from None
    key = settings.AUDIT_HASH_KEY.encode("utf-8")
    return hmac.new(key, f"phr-audit/{domain}/v1:{canonical}".encode("ascii"), hashlib.sha256).hexdigest()


def record_audit_event(actor, action, target_id, result, reason=None):
    if action not in ALLOWED_ACTIONS or result not in ALLOWED_RESULTS:
        raise InvalidAuditEvent("Unknown audit action or result")
    if reason is not None and _REASON.fullmatch(reason) is None:
        raise InvalidAuditEvent("Audit reasons must be stable non-sensitive codes")
    return AuditEvent.objects.create(
        actor_hash=_hash("actor", actor),
        action=action,
        target_hash=_hash("target", target_id),
        result=result,
        reason_code=reason or "",
    )
