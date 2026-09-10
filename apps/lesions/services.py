"""Explicit, source-checked operations on patient-owned lesion identities."""

from copy import deepcopy
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.models import Document, UploadBatch
from apps.patients.access import Capability, authorize_patient

from .models import (Lesion, LesionMatchProposal, LesionObservation, LesionObservationRevision,
                     LesionOperation, LesionProposalRevision, LesionRevision)
from .readmodels import assignment_state, lesion_state, observation_material, proposal_material, proposal_state


def _lock_documents(patient):
    """The caller holds Patient; take all batch locks before any document lock."""
    query = Document.objects.filter(patient=patient)
    tuple(UploadBatch.objects.select_for_update(of=("self",)).filter(
        pk__in=query.values_list("batch_id", flat=True),
    ).order_by("pk"))
    return {str(row.pk): row for row in query.select_for_update(of=("self",)).order_by("pk")}


def _identity(value):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("记录标识无效。") from None


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValidationError("请输入不超过 120 字的观察名称。")
    return value


def _check_observation(rows, identity, *, revision, source, require_usable=True):
    row = rows.get(_identity(identity))
    if row is None:
        raise PermissionDenied("观察来源不属于当前患者或已不可用。")
    if type(revision) is not int or revision != row["revision_number"]:
        raise ValidationError("观察关联已更新，请刷新后重试。")
    if source != row["source_token"] or (require_usable and (not row["source_usable"] or not isinstance(source, str))):
        raise ValidationError("观察来源已变化或尚未核对，请先核对原件。")
    return row


def _observation_record(patient, row):
    record = LesionObservation.objects.select_for_update().filter(pk=row["id"], patient=patient).first()
    if record is None:
        record = LesionObservation(id=row["id"], patient=patient, report_id=row["report_id"],
                                   document_id=row["document_id"], original_report_id=row["report_id"],
                                   entity_key=row["entity_key"])
        record.full_clean()
        record.save()
    return record


def _append_name(lesion, operation, after):
    before = lesion_state(lesion)
    before = {key: before[key] for key in ("name", "active")}
    revision = LesionRevision.objects.create(lesion=lesion, operation=operation,
        sequence=lesion.revision_number + 1, before=before, after=deepcopy(after))
    lesion.revision_number = revision.sequence
    lesion.save(update_fields=["revision_number"])
    return revision


def _append_assignment(observation, operation, after):
    revision = LesionObservationRevision.objects.create(observation=observation, operation=operation,
        lesion_id=after["lesion_id"], sequence=observation.revision_number + 1,
        before=assignment_state(observation), after=deepcopy(after))
    observation.revision_number = revision.sequence
    observation.save(update_fields=["revision_number"])
    return revision


def _append_proposal(proposal, operation, after):
    revision = LesionProposalRevision.objects.create(proposal=proposal, operation=operation,
        sequence=proposal.revision_number + 1, before=proposal_state(proposal), after=deepcopy(after))
    proposal.revision_number = revision.sequence
    proposal.save(update_fields=["revision_number"])
    return revision


def _confirmed_assignment(lesion, row):
    return {"status": "CONFIRMED", "lesion_id": str(lesion.pk), "source_binding": deepcopy(row["source_binding"])}


def _invalidate(documents, document_ids):
    from apps.exports.services import invalidate_document_exports

    for identity in sorted(set(document_ids)):
        document = documents.get(identity)
        if document is not None:
            invalidate_document_exports(document)


def create_lesion(patient, *, actor, observation_id, expected_revision, expected_source,
                  name, checked_original):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        if checked_original is not True:
            raise ValidationError("请查看原件并明确确认这次关联。")
        name = _name(name)
        documents = _lock_documents(access.patient)
        rows = {row["id"]: row for row in observation_material(access.patient)}
        row = _check_observation(rows, observation_id, revision=expected_revision, source=expected_source)
        if row["assignment"]["lesion_id"] is not None:
            raise ValidationError("观察已有病灶标识，请使用改派或拆分。")
        observation = _observation_record(access.patient, row)
        lesion = Lesion.objects.create(patient=access.patient, created_by=access.actor, original_name=name)
        operation = LesionOperation.objects.create(patient=access.patient, author=access.actor,
                                                   action="CREATE", checked_original=True)
        _append_name(lesion, operation, {"name": name, "active": True})
        _append_assignment(observation, operation, _confirmed_assignment(lesion, row))
        _invalidate(documents, [row["document_id"]])
        from apps.operations.audit import record_audit_event
        record_audit_event(access.actor, "lesion_created", lesion.pk, "succeeded", patient_id=access.patient.pk)
        return operation


def rename_lesion(patient, *, actor, lesion_id, expected_revision, name):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        name = _name(name)
        documents = _lock_documents(access.patient)
        rows = observation_material(access.patient, include_unavailable=True)
        lesion = _locked_lesions(access.patient, {_identity(lesion_id)},
                                 {_identity(lesion_id): expected_revision})[_identity(lesion_id)]
        state = lesion_state(lesion)
        if name == state["name"]:
            raise ValidationError("名称没有变化。")
        operation = LesionOperation.objects.create(patient=access.patient, author=access.actor, action="RENAME")
        _append_name(lesion, operation, {"name": name, "active": state["active"]})
        _invalidate(documents, _graph_documents(rows, {str(lesion.pk)}))
        from apps.operations.audit import record_audit_event
        record_audit_event(access.actor, "lesion_renamed", lesion.pk, "succeeded", patient_id=access.patient.pk)
        return operation


def _locked_lesions(patient, identities, expectations):
    if not isinstance(expectations, dict):
        raise ValidationError("请刷新病灶版本后重试。")
    rows = {str(row.pk): row for row in Lesion.objects.select_for_update().filter(
        patient=patient, pk__in=identities,
    ).order_by("pk")}
    if set(rows) != set(identities):
        raise PermissionDenied("病灶不属于当前患者或已不可用。")
    for identity, lesion in rows.items():
        expected = expectations.get(identity)
        if type(expected) is not int or expected != lesion.revision_number or not lesion_state(lesion)["active"]:
            raise ValidationError("病灶已有更新，请刷新后重试。")
    return rows


def _graph_documents(rows, identities):
    return {row["document_id"] for row in rows if row["lesion_id"] in identities and row["document_id"]}


def _checked_rows(patient, identities, expectations, *, require_usable=True):
    if not isinstance(expectations, dict) or set(expectations) != set(identities):
        raise ValidationError("请提交每个观察的核对版本和来源。")
    material = observation_material(patient, include_unavailable=True)
    lookup = {row["id"]: row for row in material}
    rows = []
    for identity in sorted(identities):
        expected = expectations[identity]
        if not isinstance(expected, dict) or set(expected) != {"revision", "source"}:
            raise ValidationError("观察核对版本无效。")
        rows.append(_check_observation(lookup, identity, revision=expected["revision"], source=expected["source"],
                                       require_usable=require_usable))
    return material, rows


def _relations_audit(access, operation):
    from apps.operations.audit import record_audit_event
    record_audit_event(access.actor, "lesion_relations_changed", operation.pk, "succeeded", patient_id=access.patient.pk)


def generate_proposals(patient, *, actor):
    from .proposals import propose_matches

    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        documents = _lock_documents(access.patient)
        material = observation_material(access.patient)
        lookup = {row["id"]: row for row in material}
        existing = set(LesionMatchProposal.objects.filter(patient=access.patient).values_list("fingerprint", flat=True))
        candidates = propose_matches(material)
        operation = None
        changed_documents = set()
        for candidate in candidates:
            if candidate.fingerprint in existing:
                continue
            if operation is None:
                operation = LesionOperation.objects.create(patient=access.patient, author=access.actor, action="PROPOSE")
            proposal = LesionMatchProposal.objects.create(patient=access.patient,
                first=_observation_record(access.patient, lookup[candidate.first_id]),
                second=_observation_record(access.patient, lookup[candidate.second_id]),
                first_binding=candidate.first_binding, second_binding=candidate.second_binding,
                reasons=list(candidate.reasons), blockers=list(candidate.blockers),
                rule_version=candidate.rule_version, fingerprint=candidate.fingerprint)
            _append_proposal(proposal, operation, {"status": "PENDING"})
            changed_documents.update(lookup[identity]["document_id"] for identity in (candidate.first_id, candidate.second_id))
        if operation is not None:
            _invalidate(documents, changed_documents)
            _relations_audit(access, operation)
        fingerprints = {candidate.fingerprint for candidate in candidates}
        return [row for row in proposal_material(access.patient, observations=material)
                if row["fingerprint"] in fingerprints]


def decide_proposal(patient, *, actor, proposal_id, action, expected_revision, expected_fingerprint,
                     expectations=None, checked_original=False, name=None, target_lesion_id=None,
                     expected_lesion_revisions=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        documents = _lock_documents(access.patient)
        proposal = LesionMatchProposal.objects.select_for_update().filter(
            patient=access.patient, pk=_identity(proposal_id)).first()
        if proposal is None:
            raise PermissionDenied("关联提议不属于当前患者或已不可用。")
        if (type(expected_revision) is not int or proposal.revision_number != expected_revision
                or proposal.fingerprint != expected_fingerprint):
            raise ValidationError("关联提议已更新，请刷新后重试。")
        material = observation_material(access.patient, include_unavailable=True)
        lookup = {row["id"]: row for row in material}
        for identity, binding in ((proposal.first_id, proposal.first_binding), (proposal.second_id, proposal.second_binding)):
            row = lookup.get(str(identity))
            if row is None or row["status"] == "UNAVAILABLE" or row["source_binding"] != binding:
                raise ValidationError("提议来源已变化，请重新生成并核对当前原件。")
        state = proposal_state(proposal)
        if state["status"] not in {"PENDING", "REJECTED", "DEFERRED"}:
            raise ValidationError("已确认的提议请通过撤销、改派或拆分调整。")
        if action == "CONFIRM":
            from .proposals import RULE_VERSION
            if proposal.rule_version != RULE_VERSION:
                raise ValidationError("提议规则已更新，请按当前原件重新生成提议；旧决定和依据仍保留。")
            if checked_original is not True:
                raise ValidationError("请查看两端原件并明确确认关联。")
            identities = {str(proposal.first_id), str(proposal.second_id)}
            material, rows = _checked_rows(access.patient, identities, expectations)
            operation = _match_locked(access, documents, material, rows, name=name,
                target_lesion_id=target_lesion_id, expected_lesion_revisions=expected_lesion_revisions)
            effects = list(operation.observation_revisions.all())
            after = {"status": "CONFIRMED", "lesion_id": str(effects[0].lesion_id),
                     "observation_revisions": {str(effect.observation_id): effect.sequence for effect in effects}}
        elif action in {"REJECT", "DEFER"}:
            after = {"status": {"REJECT": "REJECTED", "DEFER": "DEFERRED"}[action]}
            if after["status"] == state["status"]:
                raise ValidationError("提议核对状态没有变化。")
            operation = LesionOperation.objects.create(patient=access.patient, author=access.actor, action=action)
        else:
            raise ValidationError("提议核对操作无效。")
        _append_proposal(proposal, operation, after)
        if action != "CONFIRM":
            _invalidate(documents, {lookup[str(identity)]["document_id"] for identity in (proposal.first_id, proposal.second_id)})
        _relations_audit(access, operation)
        return operation


def _match_locked(access, documents, material, rows, *, name, target_lesion_id, expected_lesion_revisions):
    """Caller has authorized/locked patient and documents and checked both sources."""
    old_ids = {row["lesion_id"] for row in rows if row["lesion_id"]}
    if target_lesion_id is None:
        if old_ids:
            raise ValidationError("观察已有稳定标识，请明确选择保留哪个标识。")
        name = _name(name)
        existing = {}
        target = Lesion.objects.create(patient=access.patient, created_by=access.actor, original_name=name)
    else:
        target_lesion_id = _identity(target_lesion_id)
        if target_lesion_id not in old_ids:
            raise ValidationError("目标标识必须有本次选定的一端作为明确来源。")
        existing = _locked_lesions(access.patient, old_ids, expected_lesion_revisions)
        target = existing[target_lesion_id]
    action = "REASSIGN" if old_ids - {str(target.pk)} else "MATCH"
    operation = LesionOperation.objects.create(patient=access.patient, author=access.actor,
                                               action=action, checked_original=True)
    for identity in sorted(existing):
        state = lesion_state(existing[identity])
        _append_name(existing[identity], operation, {"name": state["name"], "active": state["active"]})
    if not existing:
        _append_name(target, operation, {"name": name, "active": True})
    for row in rows:
        observation = _observation_record(access.patient, row)
        _append_assignment(observation, operation, _confirmed_assignment(target, row))
    _invalidate(documents, _graph_documents(material, old_ids) | {row["document_id"] for row in rows})
    return operation


def match_observations(patient, *, actor, first_id, second_id, expectations, checked_original,
                       name=None, target_lesion_id=None, expected_lesion_revisions=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        if checked_original is not True:
            raise ValidationError("请查看两端原件并明确确认关联。")
        identities = {_identity(first_id), _identity(second_id)}
        if len(identities) != 2:
            raise ValidationError("请选择两个不同的报告内观察。")
        documents = _lock_documents(access.patient)
        material, rows = _checked_rows(access.patient, identities, expectations)
        operation = _match_locked(access, documents, material, rows, name=name,
            target_lesion_id=target_lesion_id, expected_lesion_revisions=expected_lesion_revisions)
        _relations_audit(access, operation)
        return operation


def split_observations(patient, *, actor, lesion_id, expected_revision, observation_ids,
                       expectations, name, checked_original):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        if checked_original is not True:
            raise ValidationError("请查看原件并确认要拆分的观察。")
        name = _name(name)
        if not isinstance(observation_ids, (list, tuple)) or not observation_ids:
            raise ValidationError("请选择要拆分的观察。")
        identities = {_identity(value) for value in observation_ids}
        if len(identities) != len(observation_ids):
            raise ValidationError("拆分观察不能重复。")
        lesion_id = _identity(lesion_id)
        documents = _lock_documents(access.patient)
        material, rows = _checked_rows(access.patient, identities, expectations)
        old = _locked_lesions(access.patient, {lesion_id}, {lesion_id: expected_revision})[lesion_id]
        if any(row["lesion_id"] != lesion_id for row in rows):
            raise ValidationError("所选观察已经改派，请刷新后重试。")
        all_ids = {row["id"] for row in material if row["lesion_id"] == lesion_id}
        if not identities < all_ids:
            raise ValidationError("拆分需保留至少一个观察在原病灶标识中。")
        new = Lesion.objects.create(patient=access.patient, created_by=access.actor, original_name=name)
        operation = LesionOperation.objects.create(patient=access.patient, author=access.actor,
                                                   action="SPLIT", checked_original=True)
        old_state = lesion_state(old)
        _append_name(old, operation, {"name": old_state["name"], "active": True})
        _append_name(new, operation, {"name": name, "active": True})
        for row in rows:
            _append_assignment(_observation_record(access.patient, row), operation, _confirmed_assignment(new, row))
        _invalidate(documents, _graph_documents(material, {lesion_id}))
        _relations_audit(access, operation)
        return operation


def unlink_observations(patient, *, actor, lesion_id, expected_revision, observation_ids, expectations):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        if not isinstance(observation_ids, (list, tuple)) or not observation_ids:
            raise ValidationError("请选择要取消关联的观察。")
        identities = {_identity(value) for value in observation_ids}
        if len(identities) != len(observation_ids):
            raise ValidationError("所选观察不能重复。")
        lesion_id = _identity(lesion_id)
        documents = _lock_documents(access.patient)
        material, rows = _checked_rows(access.patient, identities, expectations, require_usable=False)
        lesion = _locked_lesions(access.patient, {lesion_id}, {lesion_id: expected_revision})[lesion_id]
        if any(row["lesion_id"] != lesion_id for row in rows):
            raise ValidationError("所选观察已取消关联或已改派，请刷新后重试。")
        operation = LesionOperation.objects.create(patient=access.patient, author=access.actor, action="UNLINK")
        state = lesion_state(lesion)
        _append_name(lesion, operation, {"name": state["name"], "active": state["active"]})
        for row in rows:
            _append_assignment(_observation_record(access.patient, row), operation,
                                {"status": "UNASSIGNED", "lesion_id": None, "source_binding": None})
        _invalidate(documents, _graph_documents(material, {lesion_id}))
        _relations_audit(access, operation)
        return operation


def undo_operation(patient, *, actor, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        documents = _lock_documents(access.patient)
        operation = LesionOperation.objects.select_for_update().filter(
            pk=_identity(operation_id), patient=access.patient,
        ).first()
        if operation is None:
            raise PermissionDenied("操作不属于当前患者或已不可用。")
        name_effects = list(operation.lesion_revisions.select_related("lesion").order_by("lesion_id"))
        observation_effects = list(operation.observation_revisions.select_related("observation").order_by("observation_id"))
        proposal_effects = list(operation.proposal_revisions.select_related("proposal").order_by("proposal_id"))
        if not name_effects and not observation_effects and not proposal_effects:
            raise ValidationError("该操作没有可撤销的关联修订。")
        lesion_ids = {str(effect.lesion_id) for effect in name_effects}
        lesions = {str(row.pk): row for row in Lesion.objects.select_for_update().filter(
            patient=access.patient, pk__in=lesion_ids,
        ).order_by("pk")}
        observation_ids = {str(effect.observation_id) for effect in observation_effects}
        observations = {str(row.pk): row for row in LesionObservation.objects.select_for_update().filter(
            patient=access.patient, pk__in=observation_ids,
        ).order_by("pk")}
        proposal_ids = {str(effect.proposal_id) for effect in proposal_effects}
        proposals = {str(row.pk): row for row in LesionMatchProposal.objects.select_for_update().filter(
            patient=access.patient, pk__in=proposal_ids,
        ).order_by("pk")}
        if set(lesions) != lesion_ids or set(observations) != observation_ids or set(proposals) != proposal_ids:
            raise ValidationError("关联历史已经变化，不能撤销。")
        for effect in name_effects:
            if lesions[str(effect.lesion_id)].revision_number != effect.sequence:
                raise ValidationError("病灶已有后续修订，不能撤销覆盖。")
        for effect in observation_effects:
            if observations[str(effect.observation_id)].revision_number != effect.sequence:
                raise ValidationError("观察已有后续修订，不能撤销覆盖。")
        for effect in proposal_effects:
            if proposals[str(effect.proposal_id)].revision_number != effect.sequence:
                raise ValidationError("提议已有后续核对，不能撤销覆盖。")
        material = observation_material(access.patient, include_unavailable=True)
        lookup = {row["id"]: row for row in material}
        for effect in observation_effects:
            if effect.before["status"] == "CONFIRMED":
                row = lookup.get(str(effect.observation_id))
                if (row is None or not row["source_usable"]
                        or effect.before["source_binding"] != row["source_binding"]):
                    raise ValidationError("来源已有修订，撤销不能恢复旧确认；请重新查看原件建立关联。")
        for effect in proposal_effects:
            if effect.before["status"] == "CONFIRMED":
                proposal = proposals[str(effect.proposal_id)]
                for identity, binding in ((proposal.first_id, proposal.first_binding), (proposal.second_id, proposal.second_binding)):
                    row = lookup.get(str(identity))
                    if row is None or not row["source_usable"] or row["source_binding"] != binding:
                        raise ValidationError("来源已变化，撤销不能恢复旧提议确认。")
        reversal = LesionOperation.objects.create(patient=access.patient, author=access.actor,
                                                  action="UNDO", reverses=operation)
        for effect in name_effects:
            _append_name(lesions[str(effect.lesion_id)], reversal, effect.before)
        for effect in observation_effects:
            _append_assignment(observations[str(effect.observation_id)], reversal, effect.before)
        for effect in proposal_effects:
            restored = deepcopy(effect.before)
            if restored["status"] == "CONFIRMED":
                restored["observation_revisions"] = {
                    identity: observations[identity].revision_number for identity in restored["observation_revisions"]}
            _append_proposal(proposals[str(effect.proposal_id)], reversal, restored)
        document_ids = _graph_documents(material, lesion_ids)
        document_ids.update(row["document_id"] for row in material if row["id"] in observation_ids and row["document_id"])
        proposal_observations = {str(identity) for proposal in proposals.values() for identity in (proposal.first_id, proposal.second_id)}
        document_ids.update(row["document_id"] for row in material if row["id"] in proposal_observations and row["document_id"])
        _invalidate(documents, document_ids)
        _relations_audit(access, reversal)
        return reversal
