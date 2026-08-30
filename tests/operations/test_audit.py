import uuid

import pytest

from apps.operations.audit import InvalidAuditEvent, record_audit_event
from apps.operations.models import AppendOnlyAuditError


pytestmark = pytest.mark.django_db


def test_audit_accepts_only_opaque_ids_stable_actions_results_and_reason_codes():
    actor = uuid.uuid4()
    target = uuid.uuid4()
    event = record_audit_event(actor, "document_deletion_requested", target, "scheduled", "user_confirmed")

    assert len(event.actor_hash) == 64 and event.actor_hash != str(actor)
    assert len(event.target_hash) == 64 and event.target_hash != str(target)
    assert event.reason_code == "user_confirmed"

    with pytest.raises(InvalidAuditEvent):
        record_audit_event(actor, "view_ocr_text", target, "succeeded")
    with pytest.raises(InvalidAuditEvent):
        record_audit_event(actor, "document_deletion_requested", target, "maybe")
    with pytest.raises(InvalidAuditEvent):
        record_audit_event(actor, "document_deletion_requested", target, "scheduled", "patient said private text")
    with pytest.raises(InvalidAuditEvent):
        record_audit_event("raw staff name", "document_deletion_requested", target, "scheduled")


def test_audit_rows_are_append_only_and_system_actor_is_supported():
    event = record_audit_event("system", "document_deletion_purged", uuid.uuid4(), "succeeded")
    event.result = "failed"
    with pytest.raises(AppendOnlyAuditError):
        event.save()
    with pytest.raises(AppendOnlyAuditError):
        event.delete()
