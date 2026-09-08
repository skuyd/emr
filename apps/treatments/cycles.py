"""Explicit cycle decisions. New cycles preserve their immutable predecessors."""

from copy import deepcopy
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.models import Document
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .models import CycleEventLink, CycleLineage, CycleRecordLink, TreatmentCycle, TreatmentEvent, TreatmentRegimen
from .readmodels import effective_event, event_token
from .services import TreatmentConflict, _append_revision, _existing_operation
from .sources import lock_source_documents
from .validation import operation_uuid, validate_day, validate_label, validate_revision


CYCLE_FIELDS = {"anchor", "anchor_precision", "ordinal", "end", "end_precision", "note"}


def _ids(values, *, minimum=1, maximum=100):
    if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= maximum:
        raise ValidationError("请选择允许数量的记录。")
    try:
        result = [uuid.UUID(str(value)) for value in values]
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("记录标识无效。") from None
    if len(result) != len(set(result)):
        raise ValidationError("同一记录不能重复分配。")
    return result


def _checked(value):
    if value is not True:
        raise ValidationError("请先核对原件或本人记录。")


def _content(*, anchor, anchor_precision, ordinal, end=None, end_precision="UNKNOWN", note="", **extra):
    anchor = validate_day(anchor, anchor_precision)
    end = validate_day(end, end_precision)
    validate_label(ordinal)
    if anchor_precision == end_precision == "DAY" and end and end < anchor:
        raise ValidationError("明确结束日期不能早于锚点。")
    if not isinstance(note, str) or len(note) > 5000 or "\x00" in note:
        raise ValidationError("周期说明超出允许范围或含无效字符。")
    return {"anchor": anchor, "anchor_precision": anchor_precision, "ordinal": ordinal,
            "end": end, "end_precision": end_precision, "note": note.strip(),
            "status": "CONFIRMED", "recorded_as": "USER", "anchor_role": "USER_SELECTED", **extra}


def _regimen(patient, value):
    if value is None or value == "":
        return None
    identity = _ids([value])[0]
    row = TreatmentRegimen.objects.select_for_update().filter(patient=patient, pk=identity).first()
    if row is None:
        raise PermissionDenied("方案不可用。")
    if row.current_content.get("status") in {"REJECTED", "SUPERSEDED"}:
        raise TreatmentConflict("方案已失效，请刷新后重新选择。")
    from .readmodels import _trusted_material
    current = next(item for item in _trusted_material(patient)["regimens"] if item["id"] == str(row.pk))
    if not current["source_valid"]:
        raise TreatmentConflict("方案来源已变化，请刷新后重新选择。")
    return row


def _locked_events(patient, event_ids):
    identities = _ids(list(event_ids))
    found = list(TreatmentEvent.objects.filter(patient=patient, pk__in=identities).order_by("pk"))
    if len(found) != len(identities):
        raise PermissionDenied("治疗记录不可用。")
    # Regimen and automatic boundary context can reference other documents.
    # Acquire every batch/document before the first treatment child lock.
    lock_source_documents(patient, Document.objects.filter(patient=patient, deleted_at__isnull=True).values_list("pk", flat=True))
    events = list(TreatmentEvent.objects.select_for_update().filter(patient=patient, pk__in=identities).order_by("pk"))
    rows = {str(event.pk): effective_event(event) for event in events}
    if any(not row["source_valid"] or row["status"] in {"REJECTED", "SUPERSEDED"} for row in rows.values()):
        raise TreatmentConflict("治疗来源已变化或已拒绝，请重新核对。")
    return events, rows


def _source_tokens(rows):
    return {identity: event_token(row) for identity, row in rows.items()}


def _locked_cycles(patient, cycle_ids, *, replacing_regimen=False):
    identities = _ids(cycle_ids)
    found = list(TreatmentCycle.objects.filter(patient=patient, pk__in=identities).order_by("pk"))
    if len(found) != len(identities):
        raise PermissionDenied("周期不可用。")
    links = list(CycleEventLink.objects.filter(cycle__in=found).order_by("pk"))
    events, rows = _locked_events(patient, sorted({link.event_id for link in links}))
    cycles = list(TreatmentCycle.objects.select_for_update().filter(patient=patient, pk__in=identities).order_by("pk"))
    input_fingerprint = None
    if any(item.origin == "AUTOMATIC" for item in cycles):
        from .input_material import trusted_input_material
        input_fingerprint = trusted_input_material(patient)["fingerprint"]
    for item in cycles:
        own_links = [link for link in links if link.cycle_id == item.pk]
        if (not own_links or any(link.source_token != event_token(rows[str(link.event_id)]) for link in own_links)
                or item.current_content.get("status") == "SUPERSEDED"
                or (item.origin == "AUTOMATIC" and (item.derivation_run_id is None
                    or item.derivation_run.input_fingerprint != input_fingerprint))):
            raise TreatmentConflict("周期已被替代或来源已变化，请刷新后重新核对。")
        if item.regimen_id and not replacing_regimen:
            regimen = _regimen(patient, item.regimen_id)
            if item.current_content.get("regimen_token") != digest({"content": regimen.current_content, "revision": regimen.revision_number}):
                raise TreatmentConflict("方案已更正，请明确重新选择并核对周期所属方案。")
    return cycles, events, rows, links


def _new_cycle(access, *, content, events, rows, regimen, operation, request_hash, action="CREATE", position=0):
    content = {**deepcopy(content), "regimen_id": str(regimen.pk) if regimen else None}
    if regimen:
        content = {**deepcopy(content), "regimen_token": digest({"content": regimen.current_content, "revision": regimen.revision_number})}
    cycle = TreatmentCycle(patient=access.patient, created_by=access.actor, origin="USER", regimen=regimen,
                           source_key=f"user:{operation}:{position}", initial_content=deepcopy(content), current_content=deepcopy(content))
    cycle.full_clean()
    cycle.save()
    for event in events:
        link = CycleEventLink(cycle=cycle, event=event, role="CONTEXT", source_token=event_token(rows[str(event.pk)]))
        link.full_clean()
        link.save()
    _append_revision(access, cycle, action=action, operation_id=operation, request_digest=request_hash,
                     before={}, after=content, checked_original=True, source_tokens=_source_tokens(rows))
    return cycle


def _check_revisions(cycles, expected):
    if not isinstance(expected, dict) or set(expected) != {str(cycle.pk) for cycle in cycles}:
        raise ValidationError("请提交每个周期的当前版本号。")
    for cycle in cycles:
        validate_revision(expected[str(cycle.pk)])
        if cycle.revision_number != expected[str(cycle.pk)]:
            raise TreatmentConflict("周期已更正，请刷新后核对当前版本。")


def create_cycle(patient, *, actor, event_ids, expected_sources, anchor, anchor_precision, ordinal,
                 regimen_id, checked_original, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identities = _ids(event_ids)
        _checked(checked_original)
        content = _content(anchor=anchor, anchor_precision=anchor_precision, ordinal=ordinal)
        request_hash = digest({"action": "CREATE_CYCLE", "events": sorted(map(str, identities)),
                               "sources": expected_sources, "content": content, "regimen": regimen_id})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return prior[0].cycle
        events, rows = _locked_events(access.patient, identities)
        if expected_sources != _source_tokens(rows):
            raise TreatmentConflict("治疗来源核对标识不一致，请刷新后重新提交。")
        regimen = _regimen(access.patient, regimen_id)
        return _new_cycle(access, content=content, events=events, rows=rows, regimen=regimen,
                          operation=operation, request_hash=request_hash)


def revise_cycle(patient, cycle_id, *, actor, action, expected_revision, operation_id,
                 checked_original=False, changes=None, expected_sources=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identities = _ids([cycle_id])
        validate_revision(expected_revision)
        if not isinstance(action, str) or action not in {"CONFIRM", "CORRECT", "REJECT", "REVOKE"}:
            raise ValidationError("请选择有效的周期操作。")
        request_hash = digest({"action": action, "cycle": str(identities[0]), "revision": expected_revision,
                               "checked_original": checked_original, "changes": changes, "sources": expected_sources})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return prior[0]
        cycles, _, rows, _ = _locked_cycles(access.patient, identities,
            replacing_regimen=action == "CORRECT" and isinstance(changes, dict) and "regimen_id" in changes)
        cycle = cycles[0]
        _check_revisions(cycles, {str(cycle.pk): expected_revision})
        if action in {"CONFIRM", "CORRECT"}:
            _checked(checked_original)
            if cycle.origin == "AUTOMATIC" and expected_sources != _source_tokens(rows):
                raise TreatmentConflict("周期来源核对标识不一致，请刷新后重新提交。")
        before, after = deepcopy(cycle.current_content), deepcopy(cycle.current_content)
        before["regimen_id"] = str(cycle.regimen_id) if cycle.regimen_id else None
        if changes and action != "CORRECT":
            raise ValidationError("修改周期内容请使用更正操作。")
        if action == "CORRECT":
            if not isinstance(changes, dict) or not changes or set(changes) - (CYCLE_FIELDS | {"regimen_id"}):
                raise ValidationError("更正字段无效。")
            if "regimen_id" in changes:
                cycle.regimen = _regimen(access.patient, changes["regimen_id"])
                cycle.save(update_fields=["regimen"])
                after["regimen_id"] = str(cycle.regimen_id) if cycle.regimen_id else None
                after["regimen_token"] = (digest({"content": cycle.regimen.current_content, "revision": cycle.regimen.revision_number})
                                          if cycle.regimen_id else None)
            after.update({key: value for key, value in changes.items() if key != "regimen_id"})
            after = _content(**after)
            after["recorded_as"] = "USER_CORRECTION"
        after["status"] = {"CONFIRM": "CONFIRMED", "CORRECT": "CONFIRMED", "REJECT": "REJECTED", "REVOKE": "PENDING"}[action]
        return _append_revision(access, cycle, action=action, operation_id=operation, request_digest=request_hash,
                                before=before, after=after, checked_original=checked_original, source_tokens=_source_tokens(rows))


def _merged_content(cycles, resolution):
    if not isinstance(resolution, dict) or set(resolution) - (CYCLE_FIELDS | {"regimen_id"}):
        raise ValidationError("请明确填写合并后冲突字段的处理结果。")
    values = {}
    for fields in [("anchor", "anchor_precision"), ("end", "end_precision"), ("ordinal",)]:
        distinct = {tuple(cycle.current_content.get(field) for field in fields) for cycle in cycles}
        if len(distinct) > 1 and any(field not in resolution for field in fields):
            raise ValidationError("日期或序号冲突，请明确选择结果或保留未知。")
        values.update({field: resolution.get(field, cycles[0].current_content.get(field)) for field in fields})
    values["note"] = resolution.get("note", "")
    regimen_ids = {cycle.regimen_id for cycle in cycles}
    if len(regimen_ids) > 1 and "regimen_id" not in resolution:
        raise ValidationError("方案冲突，请明确选择方案或保留未知。")
    return _content(**values), resolution.get("regimen_id", cycles[0].regimen_id)


def _supersede(access, parent, child_cycles, *, operation, request_hash, action):
    before = deepcopy(parent.current_content)
    after = {**deepcopy(before), "status": "SUPERSEDED"}
    _append_revision(access, parent, action=action, operation_id=operation, request_digest=request_hash,
                     before=before, after=after, checked_original=True)
    parent.record_links.filter(active=True).update(active=False)
    for child in child_cycles:
        lineage = CycleLineage(predecessor=parent, successor=child, operation_id=operation)
        lineage.full_clean()
        lineage.save()


def _copy_record(link, child):
    # The prior association remains attached to its immutable predecessor.
    # Links without an explicit split assignment remain unassigned in the active projection.
    copied = CycleRecordLink(cycle=child, document_id=link.document_id, observation_id=link.observation_id,
                             report_id=link.report_id, origin=link.origin, source_token=link.source_token, assigned=link.assigned)
    copied.full_clean()
    copied.save()


def merge_cycles(patient, *, actor, cycle_ids, expected_revisions, resolution, checked_original, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identities = _ids(cycle_ids, minimum=2, maximum=50)
        _checked(checked_original)
        request_hash = digest({"action": "MERGE", "cycles": sorted(map(str, identities)),
                               "revisions": expected_revisions, "resolution": resolution})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return next(row.cycle for row in prior if row.action == "MERGE_RESULT")
        cycles, events, rows, _ = _locked_cycles(access.patient, identities)
        _check_revisions(cycles, expected_revisions)
        content, regimen_id = _merged_content(cycles, resolution)
        merged = _new_cycle(access, content=content, events=events, rows=rows, regimen=_regimen(access.patient, regimen_id),
                            operation=operation, request_hash=request_hash, action="MERGE_RESULT")
        seen = set()
        for parent in cycles:
            for link in parent.record_links.filter(active=True):
                key = (link.document_id, link.observation_id, link.report_id, link.assigned)
                if key not in seen:
                    _copy_record(link, merged)
                    seen.add(key)
            _supersede(access, parent, [merged], operation=operation, request_hash=request_hash, action="MERGE")
        return merged


def split_cycle(patient, cycle_id, *, actor, expected_revision, parts, checked_original, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identities = _ids([cycle_id])
        validate_revision(expected_revision)
        _checked(checked_original)
        if not isinstance(parts, list) or not 2 <= len(parts) <= 50:
            raise ValidationError("拆分需要二至五十个独立部分。")
        request_hash = digest({"action": "SPLIT", "cycle": str(identities[0]), "revision": expected_revision, "parts": parts})
        prior = _existing_operation(access.patient, access.actor, operation, request_hash)
        if prior:
            return sorted((row.cycle for row in prior if row.action == "SPLIT_RESULT"),
                          key=lambda row: row.initial_content["split_position"])
        cycles, events, rows, _ = _locked_cycles(access.patient, identities)
        parent = cycles[0]
        _check_revisions(cycles, {str(parent.pk): expected_revision})
        event_map = {str(event.pk): event for event in events}
        record_map = {str(link.pk): link for link in parent.record_links.filter(active=True)}
        seen_events, seen_records, validated = set(), set(), []
        for part in parts:
            if (not isinstance(part, dict) or set(part) - (CYCLE_FIELDS | {"event_ids", "record_link_ids", "regimen_id"})
                    or not {"anchor", "anchor_precision", "ordinal", "event_ids"} <= set(part)):
                raise ValidationError("每个子周期需独立填写锚点、精度、序号和来源分配；未知请明确留空。")
            event_ids = set(map(str, _ids(part["event_ids"])))
            record_ids = set(map(str, _ids(part.get("record_link_ids", []), minimum=0)))
            if event_ids - set(event_map) or record_ids - set(record_map):
                raise PermissionDenied("拆分不能加入原周期以外的来源。")
            if event_ids & seen_events or record_ids & seen_records:
                raise ValidationError("同一来源不能重复分配给多个子周期。")
            seen_events.update(event_ids)
            seen_records.update(record_ids)
            content = _content(**{key: value for key, value in part.items() if key in CYCLE_FIELDS}, split_position=len(validated))
            regimen = _regimen(access.patient, part.get("regimen_id", parent.regimen_id))
            validated.append((content, event_ids, record_ids, regimen))
        children = []
        for content, event_ids, record_ids, regimen in validated:
            child = _new_cycle(access, content=content, events=[event_map[key] for key in sorted(event_ids)],
                               rows={key: rows[key] for key in sorted(event_ids)}, regimen=regimen, operation=operation,
                               request_hash=request_hash, action="SPLIT_RESULT", position=len(children))
            for key in sorted(record_ids):
                _copy_record(record_map[key], child)
            children.append(child)
        _supersede(access, parent, children, operation=operation, request_hash=request_hash, action="SPLIT")
        for key in sorted(set(record_map) - seen_records):
            original = record_map[key]
            unassigned = CycleRecordLink(cycle=parent, document_id=original.document_id, report_id=original.report_id,
                                         observation_id=original.observation_id, origin="USER", assigned=False,
                                         source_token=original.source_token)
            unassigned.full_clean()
            unassigned.save()
        return children
