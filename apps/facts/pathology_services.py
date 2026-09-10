"""Atomic context replacement: new candidates, immutable old sources and undo."""
from copy import deepcopy
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.exports.services import invalidate_document_exports
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient

from .clinical_context import has_context
from .clinical_readmodels import base_field_source_token, report_material, report_source_token
from .clinical_schema import FIELDS
from .clinical_services import _report, add_manual_clinical_field
from .models import Fact, FactRevision
from .readmodels import digest, effective_fact
from .revisions import FactConflict
from .molecular_schema import SCHEMA as MOLECULAR_SCHEMA, CONTEXT as MOLECULAR_CONTEXT


def affected_context_fields(seed, fields):
    """Replace the entity and all downward users, including unselected members."""
    if not has_context(seed) or FIELDS[seed.field_key].entity_kind == "report":
        raise ValidationError("本字段没有可整体替换的标本或检测关联。")
    entities = {seed.entity_key}
    while True:
        before = len(entities)
        for field in fields:
            if has_context(field) and any(binding["state"] == "BOUND" and binding["target_entity_key"] in entities
                                          for binding in field.automatic_content["entity_context"]["bindings"]):
                entities.add(field.entity_key)
        if len(entities) == before:
            break
    selected = [field for field in fields if has_context(field) and field.entity_key in entities]
    if len(selected) > 300:
        raise ValidationError("关联范围过大，请先核对并拆分报告。")
    return sorted(selected, key=lambda field: (FIELDS[field.field_key].rank, str(field.pk)))


def _history(fact):
    return [[str(r.pk), r.sequence, r.action, str(r.author_id) if r.author_id else None]
            for r in fact.revisions.order_by("sequence")]


def _head(fact):
    return {"revision_number": fact.revision_number, "source": effective_fact(fact)["current_source_token"],
            "created_by": str(fact.created_by_id) if fact.created_by_id else None, "history": _history(fact)}


def _base_guard(fact):
    return {"base_source": base_field_source_token(fact), "created_by": str(fact.created_by_id) if fact.created_by_id else None,
            "history_before": _history(fact)}


def _append(access, fact, *, status, metadata_key, metadata, guard=None):
    current = effective_fact(fact)
    before = {key: deepcopy(current[key]) for key in ("content", "status", "source_token", "context_snapshot") if key in current}
    after = {**deepcopy(before), "status": status, "source_token": current["current_source_token"], metadata_key: deepcopy(metadata)}
    if guard is not None:
        after["context_snapshot"]["replacement_source_guard"] = deepcopy(guard)
    revision = FactRevision.objects.create(fact=fact, author=access.actor, sequence=fact.revision_number + 1,
                                           action="EXCLUDE" if status == "EXCLUDED" else "UNDO", before=before, after=after,
                                           checked_original=metadata_key == "context_replacement", source=current["source"])
    fact.revision_number += 1
    fact.save(update_fields=["revision_number"])
    record_audit_event(access.actor.pk, "fact_revised", fact.pk, "succeeded", metadata_key, patient_id=access.patient.pk)
    return revision


def _replace(access, report, seed, all_fields, replacements):
    selected = affected_context_fields(seed, all_fields)
    selected_ids = {str(f.pk) for f in selected}
    if not isinstance(replacements, list) or not replacements or len(replacements) != len(selected):
        raise ValidationError("请为关联范围内的全部原字段提供替代项，不能漏掉未选评分。")
    entries = {}
    selected_by_id = {str(f.pk): f for f in selected}
    for entry in replacements:
        original = selected_by_id.get(entry.get("old_fact_id")) if isinstance(entry, dict) else None
        keys = {"old_fact_id", "value", "fragments", "bindings"}
        if original and original.schema_version == MOLECULAR_SCHEMA:
            keys |= {"association", "reported_assertion"}
        if not isinstance(entry, dict) or set(entry) != keys:
            raise ValidationError("关联替代项形状无效。")
        identity = entry["old_fact_id"]
        if not isinstance(identity, str) or identity not in selected_ids or identity in entries:
            raise ValidationError("替代项存在重复、遗漏或其他报告字段。")
        entries[identity] = entry
    if set(entries) != selected_ids:
        raise ValidationError("必须整体替换受影响的字段集合。")
    current_by_id = {str(f.pk): f for f in all_fields}
    new_entities = {f.entity_key: FIELDS[f.field_key].entity_kind + ":" + uuid.uuid4().hex for f in selected}
    created = {}
    for old in selected:
        entry = entries[str(old.pk)]
        bindings = deepcopy(entry["bindings"])
        if not isinstance(bindings, list):
            raise ValidationError("请逐个声明替代项的关联。")
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"role", "state", "target_fact_id", "target_entity_key", "proof_fragment_ordinals", "reason"}:
                raise ValidationError("替代关联形状无效。")
            if binding["state"] == "BOUND":
                target = created.get(binding["target_fact_id"]) or current_by_id.get(binding["target_fact_id"])
                if target is None or (str(target.pk) in selected_ids and binding["target_fact_id"] not in created):
                    raise ValidationError("替代关联必须引用已建立的低层锚。")
                binding.update(target_fact_id=str(target.pk), target_entity_key=target.entity_key)
        context = {"context_version": "IHC_CONTEXT_V1", "report_id": str(report.pk), "membership_policy": "IHC_CONTEXT_V1", "bindings": bindings}
        if old.schema_version == MOLECULAR_SCHEMA:
            context.update(context_version=MOLECULAR_CONTEXT, membership_policy=MOLECULAR_CONTEXT,
                           association=deepcopy(entry["association"]))
        new = add_manual_clinical_field(access.patient, actor=access.actor, report_id=report.pk,
                                        entity_key=new_entities[old.entity_key], field_key=old.field_key, value=entry["value"],
                                        fragments=entry["fragments"], expected_report_source=report_source_token(report),
                                        entity_context=context, source_role=old.automatic_content["source_role"],
                                        reported_assertion=entry.get("reported_assertion"))
        created[str(old.pk)] = new
    metadata = {"operation_id": str(uuid.uuid4()), "replacement_fact_ids": sorted(str(f.pk) for f in created.values())}
    guard = {"actor_id": str(access.actor.pk), "base_sources": {str(f.pk): _base_guard(f) for f in [*selected, *created.values()]},
             "unaffected_heads": {str(f.pk): _head(f) for f in all_fields if str(f.pk) not in selected_ids}}
    # High-level users first: their saved before snapshots still describe the
    # original confirmed dependency graph, before its anchors are excluded.
    events = {}
    for old in reversed(selected):
        events[str(old.pk)] = _append(access, old, status="EXCLUDED", metadata_key="context_replacement", metadata=metadata, guard=guard)
    return events[str(seed.pk)]


def _undo(access, report, seed, all_fields):
    latest = seed.revisions.order_by("-sequence").first()
    metadata = latest.after.get("context_replacement") if latest else None
    if not metadata:
        raise ValidationError("没有可整体撤销的关联替换；已撤销的操作不能复活。")
    operation_id = metadata["operation_id"]
    events = list(FactRevision.objects.filter(fact__clinical_report=report, after__context_replacement__operation_id=operation_id))
    originals = {str(event.fact_id): event for event in events}
    all_by_id = {str(f.pk): f for f in all_fields}
    new_ids = set(metadata["replacement_fact_ids"])
    guard = latest.after["context_snapshot"]["replacement_source_guard"]
    if not originals or not new_ids <= all_by_id.keys():
        raise FactConflict("替代字段或审计范围已变化。")
    for identity, event in originals.items():
        current = all_by_id.get(identity)
        head = current.revisions.order_by("-sequence").first() if current else None
        if (head is None or head.pk != event.pk or current.revision_number != event.sequence
                or event.after.get("context_replacement") != metadata or str(event.author_id) != guard["actor_id"]):
            raise FactConflict("原字段已有其他修改，不能整体撤销覆盖。")
    if any(all_by_id[identity].revision_number != 0 or all_by_id[identity].revisions.exists() for identity in new_ids):
        raise FactConflict("替代字段已经核对或修改，不能整体撤销覆盖。")
    for identity, frozen in guard["base_sources"].items():
        current = all_by_id.get(identity)
        actual = _base_guard(current) if current else None
        if actual and identity in originals:
            actual["history_before"] = actual["history_before"][:-1]
        if actual != frozen:
            raise FactConflict("原始来源或作者身份已变化，不能用撤销恢复关联。")
    outside = {str(f.pk): _head(f) for f in all_fields if str(f.pk) not in originals and str(f.pk) not in new_ids}
    if outside != guard["unaffected_heads"]:
        raise FactConflict("关联上下文或其他来源头已变化，请重新核对。")
    undone = {"operation_id": operation_id}
    for identity in sorted(new_ids):
        _append(access, all_by_id[identity], status="EXCLUDED", metadata_key="context_replacement_undo", metadata=undone)
    restored = {}
    for old in sorted((all_by_id[identity] for identity in originals), key=lambda f: (FIELDS[f.field_key].rank, str(f.pk))):
        restored[str(old.pk)] = _append(access, old, status="PENDING", metadata_key="context_replacement_undo", metadata=undone)
    return restored[str(seed.pk)]


def revise_context_group(patient, fact_id, *, actor, action, expected_revision, expected_source, changes, checked_original=False):
    if actor is None:
        raise PermissionDenied("关联替换须记录实际操作者。")
    keys = {"expected_material", "replacements"} if action == "REPLACE_CONTEXT" else {"expected_material"}
    if not isinstance(changes, dict) or set(changes) != keys or not isinstance(changes["expected_material"], str):
        raise ValidationError("请携带完整报告当前核对身份。")
    if action == "REPLACE_CONTEXT" and checked_original is not True:
        raise ValidationError("关联替换须逐项对照原件，替代项仍需要新的核对。")
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        identity = Fact.objects.filter(pk=fact_id, document__patient=access.patient).values_list("clinical_report_id", flat=True).first()
        if identity is None:
            raise PermissionDenied
        report = _report(access, identity)
        # Every writer already holds the document mutex. Lock affected rows in
        # one UUID order before a grouped write or author-lifecycle interaction.
        list(Fact.objects.select_for_update().filter(clinical_report=report).order_by("pk"))
        all_fields = list(report.fields.select_related("document__patient__account", "document_page", "parsing_version", "evidence"))
        for field in all_fields:
            field.clinical_report = report
        seed = next((f for f in all_fields if str(f.pk) == str(fact_id)), None)
        if seed is None or not has_context(seed):
            raise ValidationError("仅新病理/IHC 字段支持关联替换。")
        current = effective_fact(seed)
        if (type(expected_revision) is not int or expected_revision != seed.revision_number or not current["source_valid"]
                or expected_source != current["current_source_token"]
                or changes["expected_material"] != digest(report_material(access.patient, report_ids=[report.pk]))):
            raise FactConflict("字段、报告或关联来源已变化，请刷新后逐项核对。")
        result = _replace(access, report, seed, all_fields, changes["replacements"]) if action == "REPLACE_CONTEXT" else _undo(access, report, seed, all_fields)
        invalidate_document_exports(report.document)
        return result
