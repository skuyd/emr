"""Patient decisions about source wording and regimen grouping, not prescriptions."""
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.patients.access import authorize_patient

from .cycles import _checked, _ids, _locked_events, _source_tokens
from .input_material import trusted_input_material
from .models import TreatmentRegimen
from .services import TreatmentConflict, _append_revision, _existing_operation
from .signals import digest, normalized_regimen
from .validation import operation_uuid, validate_revision


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
        raise ValidationError("请填写已核对的方案名称或原文，最多1000字。")
    return value.strip()


def create_regimen(patient, *, actor, text, event_ids, expected_sources, checked_original, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        title = _text(text)
        _checked(checked_original)
        identities = _ids(event_ids, minimum=0)
        request_hash = digest({"action": "CREATE_REGIMEN", "text": title, "events": sorted(map(str, identities)), "sources": expected_sources})
        previous = _existing_operation(access.patient, access.actor, operation, request_hash)
        if previous:
            return previous[0].regimen
        events, rows = _locked_events(access.patient, identities) if identities else ([], {})
        tokens = _source_tokens(rows)
        if expected_sources != tokens:
            raise TreatmentConflict("治疗来源已变化，请刷新后核对方案依据。")
        content = {"text": title, "status": "CONFIRMED", "actual_start": None, "actual_end": None,
                   "event_tokens": tokens, "note": "", "cadence": None, "recorded_as": "USER"}
        row = TreatmentRegimen(patient=access.patient, origin="USER", created_by=access.actor,
            source_key=f"user:{operation}", normalized_key=digest(normalized_regimen(title)),
            episode_key=digest({"patient": access.patient.pk, "operation": operation}),
            initial_content=deepcopy(content), current_content=deepcopy(content))
        row.full_clean()
        row.save()
        row.events.add(*events)
        _append_revision(access, row, action="CREATE", operation_id=operation, request_digest=request_hash,
                         before={}, after=content, checked_original=True, source_tokens=tokens)
        return row


def revise_regimen(patient, regimen_id, *, actor, action, expected_revision, operation_id,
                    checked_original=False, changes=None, expected_sources=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identity = _ids([regimen_id])[0]
        validate_revision(expected_revision)
        if not isinstance(action, str) or action not in {"CONFIRM", "CORRECT", "REJECT", "REVOKE"}:
            raise ValidationError("请选择有效的方案操作。")
        request_hash = digest({"action": action, "regimen": str(identity), "revision": expected_revision,
                               "changes": changes, "checked_original": checked_original, "sources": expected_sources})
        previous = _existing_operation(access.patient, access.actor, operation, request_hash)
        if previous:
            return previous[0]
        row = TreatmentRegimen.objects.filter(patient=access.patient, pk=identity).first()
        if row is None:
            raise PermissionDenied("方案不可用。")
        event_ids = list(row.events.values_list("pk", flat=True))
        _, events = _locked_events(access.patient, event_ids) if event_ids else ([], {})
        row = TreatmentRegimen.objects.select_for_update().get(patient=access.patient, pk=identity)
        tokens = _source_tokens(events)
        if (row.revision_number != expected_revision or row.current_content.get("status") == "SUPERSEDED"
                or tokens != row.current_content.get("event_tokens", {})
                or (row.origin == "AUTOMATIC" and row.current_content.get("input_fingerprint") != trusted_input_material(access.patient)["fingerprint"])):
            raise TreatmentConflict("方案或依据已变化，请刷新后重新核对。")
        if action in {"CONFIRM", "CORRECT"}:
            _checked(checked_original)
            if (row.origin == "AUTOMATIC" or tokens) and expected_sources != tokens:
                raise TreatmentConflict("方案来源核对标识不一致，请刷新后重新提交。")
        before, after = deepcopy(row.current_content), deepcopy(row.current_content)
        if changes and action != "CORRECT":
            raise ValidationError("修改方案内容请使用更正操作。")
        if action == "CORRECT":
            if not isinstance(changes, dict) or not changes or set(changes) - {"text", "note"}:
                raise ValidationError("更正字段无效。")
            if "text" in changes:
                after["text"] = _text(changes["text"])
            if "note" in changes:
                if not isinstance(changes["note"], str) or len(changes["note"]) > 5000 or "\x00" in changes["note"]:
                    raise ValidationError("方案说明超出允许范围或含无效字符。")
                after["note"] = changes["note"].strip()
            after["recorded_as"] = "USER_CORRECTION"
        after["status"] = {"CONFIRM": "CONFIRMED", "CORRECT": "CONFIRMED", "REJECT": "REJECTED", "REVOKE": "PENDING"}[action]
        return _append_revision(access, row, action=action, operation_id=operation, request_digest=request_hash,
                                before=before, after=after, checked_original=checked_original, source_tokens=tokens)
