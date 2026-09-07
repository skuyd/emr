"""Treatment writes serialize with revocation and preserve the actual actor."""

from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient

from .models import TreatmentCycle, TreatmentEvent, TreatmentRegimen, TreatmentRevision
from .readmodels import effective_event
from .sources import lock_source_documents
from .validation import EVENT_FIELDS, event_content, operation_uuid, validate_revision


class TreatmentConflict(ValueError):
    pass


def _existing_operation(patient, actor, operation_id, request_digest):
    prior = list(TreatmentRevision.objects.filter(patient=patient, operation_id=operation_id).order_by("pk"))
    if prior and any(row.author_id != actor.pk or row.request_digest != request_digest for row in prior):
        raise TreatmentConflict("此提交已处理且内容不一致，请刷新后重新提交。")
    return prior


def _append_revision(access, record, *, action, operation_id, request_digest, before, after,
                     checked_original=False, source_tokens=None):
    kind = {TreatmentEvent: "event", TreatmentRegimen: "regimen", TreatmentCycle: "cycle"}[type(record)]
    target = {kind: record}
    record.current_content = deepcopy(after)
    record.revision_number += 1
    record.save(update_fields=["current_content", "revision_number"])
    revision = TreatmentRevision.objects.create(
        patient=access.patient, **target, author=access.actor, sequence=record.revision_number,
        action=action, operation_id=operation_id, request_digest=request_digest,
        before=deepcopy(before), after=deepcopy(after), checked_original=checked_original,
        source_tokens=source_tokens or {},
    )
    created = action in {"CREATE", "MERGE_RESULT", "SPLIT_RESULT", "PROPOSE"}
    record_audit_event(access.actor, f"treatment_{kind}_{'created' if created else 'revised'}",
                       record.pk, "succeeded", action.lower(), patient_id=access.patient.pk,
                       resource_type=f"treatment_{kind}", request_id=operation_id)
    return revision


def create_manual_event(patient, *, actor, kind, title, occurrence, occurred_on=None, date_precision="UNKNOWN",
                        regimen_text="", cycle_ordinal=None, cycle_day=None, note="", operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        content = event_content({"kind": kind, "title": title, "occurrence": occurrence,
                                 "date": occurred_on, "date_precision": date_precision,
                                 "date_raw": occurred_on or "", "regimen_text": regimen_text,
                                 "cycle_ordinal": cycle_ordinal, "cycle_day": cycle_day, "note": note,
                                 "status": "CONFIRMED", "recorded_as": "USER"})
        request_hash = digest({"action": "CREATE_EVENT", "content": content})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return prior[0].event
        event = TreatmentEvent(patient=access.patient, created_by=access.actor, origin="USER",
                               source_key=f"user:{operation}", initial_content=deepcopy(content), current_content=deepcopy(content))
        event.full_clean()
        event.save()
        _append_revision(access, event, action="CREATE", operation_id=operation, request_digest=request_hash,
                         before={}, after=content)
        return event


def revise_event(patient, event_id, *, actor, action, expected_revision, operation_id,
                 checked_original=False, changes=None, expected_sources=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        validate_revision(expected_revision)
        identity = TreatmentEvent.objects.filter(pk=event_id, patient=access.patient).first()
        if identity is None:
            raise PermissionDenied("治疗记录不可用。")
        lock_source_documents(access.patient, identity.evidence.values_list("document_id", flat=True))
        event = TreatmentEvent.objects.select_for_update().get(pk=identity.pk, patient=access.patient)
        request_hash = digest({"action": action, "event_id": str(event.pk), "revision": expected_revision,
                               "checked_original": checked_original, "changes": changes, "sources": expected_sources})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return prior[0]
        if event.revision_number != expected_revision:
            raise TreatmentConflict("记录已更正，请刷新后核对当前版本。")
        if action not in {"CONFIRM", "CORRECT", "REJECT", "REVOKE"}:
            raise ValidationError("请选择有效的治疗记录操作。")
        effective = effective_event(event)
        if not effective["source_valid"]:
            raise TreatmentConflict("来源已变化，请重新核对。")
        tokens = {row["id"]: row["current_source_token"] for row in effective["sources"]}
        if event.origin != "USER" and expected_sources != tokens:
            raise TreatmentConflict("来源核对标识不一致，请刷新后对照原件。")
        if action in {"CONFIRM", "CORRECT"} and checked_original is not True:
            raise ValidationError("请先核对原件或本人记录。")
        if changes and action != "CORRECT":
            raise ValidationError("修改内容请使用更正操作。")
        before = deepcopy(event.current_content)
        after = deepcopy(before)
        if action == "CORRECT":
            if not isinstance(changes, dict) or not changes or set(changes) - EVENT_FIELDS:
                raise ValidationError("更正字段无效。")
            after.update(changes)
            if "date" in changes and "date_raw" not in changes:
                after["date_raw"] = changes["date"] or ""
            after = event_content(after)
            if before["occurrence"] != "OCCURRED" and after["occurrence"] == "OCCURRED":
                after["recorded_as"] = "USER_CORRECTION"
        after["status"] = {"CONFIRM": "CONFIRMED", "CORRECT": "CONFIRMED", "REJECT": "REJECTED", "REVOKE": "PENDING"}[action]
        return _append_revision(access, event, action=action, operation_id=operation, request_digest=request_hash,
                                before=before, after=after, checked_original=checked_original, source_tokens=tokens)
