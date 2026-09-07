"""Read-only automatic preview and authorized source-locked baseline creation."""

from copy import deepcopy
import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.models import Document
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient

from .cycles import revise_cycle
from .input_material import trusted_input_material
from .models import CycleEventLink, TreatmentCycle, TreatmentDerivationRun, TreatmentEvent, TreatmentEvidence, TreatmentRegimen
from .proposals import propose_cycles
from .readmodels import effective_event, event_token
from .services import TreatmentConflict
from .signals import RULE_VERSION, digest, extract_treatment_signals
from .sources import lock_source_documents
from .validation import operation_uuid


def _calculate(material):
    proposals = propose_cycles(extract_treatment_signals(material), material.get("lab_context", []),
                               event_decisions=material.get("event_decisions", []))
    regimen_map = {}
    for regimen in proposals["regimens"]:
        identity = digest({"regimen": regimen["id"], "input": material["fingerprint"]})
        regimen_map[regimen["id"]] = identity
        regimen["id"] = regimen["source_key"] = identity
    cycle_map = {}
    for cycle in proposals["cycles"]:
        cycle["regimen_id"] = regimen_map.get(cycle["regimen_id"])
        identity = digest({"cycle": cycle["id"], "input": material["fingerprint"]})
        cycle_map[cycle["id"]] = identity
        cycle["id"] = cycle["source_key"] = identity
    for cycle in proposals["cycles"]:
        for conflict in cycle["conflicts"]:
            conflict["cycle_id"] = cycle_map[conflict["cycle_id"]]
    proposals["fingerprint"] = digest({key: value for key, value in proposals.items() if key != "fingerprint"})
    return proposals


def proposal_preview(patient, *, actor):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        material = trusted_input_material(access.patient)
        proposals = _calculate(material)
        previous = {row.source_key: row for row in TreatmentCycle.objects.filter(patient=access.patient)}
        for proposal in proposals["cycles"]:
            existing = previous.get(proposal["source_key"])
            status = existing.current_content["status"] if existing else "PENDING"
            proposal.update({"persisted_id": str(existing.pk) if existing else None,
                             "revision_number": existing.revision_number if existing else 0,
                             "decision_status": status, "decided": status in {"CONFIRMED", "REJECTED", "SUPERSEDED"}})
        if trusted_input_material(access.patient)["fingerprint"] != material["fingerprint"]:
            raise TreatmentConflict("治疗来源正在更新，请刷新后查看当前提议。")
        result = {"input_fingerprint": material["fingerprint"], "source_manifest_hash": material["source_manifest_hash"],
                  "proposals": proposals, "source_count": len(material["sources"])}
    authorize_patient(access.patient, actor, "read")
    return result


def _locked_input(access, expected_fingerprint):
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
        raise ValidationError("提议来源标识无效，请刷新后重新核对。")
    lock_source_documents(access.patient, Document.objects.filter(patient=access.patient, deleted_at__isnull=True).values_list("pk", flat=True))
    material = trusted_input_material(access.patient)
    if material["fingerprint"] != expected_fingerprint:
        raise TreatmentConflict("治疗来源或规则已变化，请刷新后核对新的自动提议。")
    return material


def _persist_evidence(event, proof):
    evidence = TreatmentEvidence(
        event=event, document_id=proof["document_id"], document_page_id=proof["page_id"],
        parsing_version_id=proof.get("parsing_version"), fact_id=proof.get("fact_id"),
        source_evidence_id=proof.get("source_evidence_id"), source_token=proof["source_token"],
        source_revision=proof["source_revision"], lifecycle_revision=proof["lifecycle_revision"],
        material_revision=proof["material_revision"], start_offset=proof["start_offset"], end_offset=proof["end_offset"],
        raw_text=proof["raw_text"], source=deepcopy(proof),
    )
    evidence.full_clean()
    evidence.save()


def _persist_locked(access, material, proposals, operation):
    request_hash = digest({"action": "PERSIST_PROPOSALS", "fingerprint": material["fingerprint"]})
    previous_operation = TreatmentDerivationRun.objects.filter(patient=access.patient, operation_id=operation).first()
    if previous_operation and (previous_operation.requested_by_id != access.actor.pk or previous_operation.request_digest != request_hash):
        raise TreatmentConflict("此次提交已经处理且内容不一致，请刷新后重新提交。")
    existing = TreatmentDerivationRun.objects.filter(patient=access.patient, rule_version=RULE_VERSION,
                                                     input_fingerprint=material["fingerprint"]).first()
    if existing:
        return existing
    run = TreatmentDerivationRun.objects.create(patient=access.patient, rule_version=RULE_VERSION,
        input_fingerprint=material["fingerprint"], source_manifest_hash=material["source_manifest_hash"],
        requested_by=access.actor, access_revision=access.membership.revision,
        result_counts={key: len(proposals[key]) for key in ("events", "regimens", "cycles")},
        operation_id=operation, request_digest=request_hash)
    events = {}
    for row in proposals["events"]:
        if row.get("model_id"):
            event = TreatmentEvent.objects.get(patient=access.patient, pk=row["model_id"])
            events[row["id"]] = event
            continue
        event, created = TreatmentEvent.objects.get_or_create(patient=access.patient, source_key=row["source_key"], defaults={
            "origin": "AUTOMATIC", "created_by": access.actor, "rule_version": RULE_VERSION,
            "initial_content": deepcopy(row["content"]), "current_content": deepcopy(row["content"]),
        })
        if created:
            for proof in row["sources"]:
                _persist_evidence(event, proof)
        events[row["id"]] = event
    regimens = {}
    for row in proposals["regimens"]:
        event_links = {str(events[key].pk): event_token(effective_event(events[key])) for key in row["event_ids"]}
        content = {**deepcopy(row["content"]), "event_tokens": event_links,
                   "cadence": deepcopy(row["cadence"]), "lab_periodicity": deepcopy(row.get("lab_periodicity", [])),
                   "input_fingerprint": material["fingerprint"]}
        regimen, created = TreatmentRegimen.objects.get_or_create(patient=access.patient, source_key=row["source_key"], defaults={
            "origin": "AUTOMATIC", "created_by": access.actor, "normalized_key": row["normalized_key"],
            "episode_key": row["episode_key"], "initial_content": deepcopy(content), "current_content": content,
        })
        if created:
            regimen.events.add(*[events[key] for key in row["event_ids"]])
        regimens[row["id"]] = regimen
    for row in proposals["cycles"]:
        regimen = regimens.get(row["regimen_id"])
        content = {**deepcopy(row["content"]), "conflicts": deepcopy(row["conflicts"])}
        content["regimen_id"] = str(regimen.pk) if regimen else None
        if regimen:
            content["regimen_token"] = digest({"content": regimen.current_content, "revision": regimen.revision_number})
        cycle, created = TreatmentCycle.objects.get_or_create(patient=access.patient, source_key=row["source_key"], defaults={
            "origin": "AUTOMATIC", "created_by": access.actor, "regimen": regimen, "derivation_run": run,
            "initial_content": deepcopy(content), "current_content": content,
        })
        if created:
            for key in row["event_ids"]:
                event = events[key]
                link = CycleEventLink(cycle=cycle, event=event, role="CONTEXT", source_token=event_token(effective_event(event)))
                link.full_clean()
                link.save()
    record_audit_event(access.actor, "treatment_derivation_created", access.patient.pk, "succeeded",
                       patient_id=access.patient.pk, resource_type="patient", request_id=operation)
    return run


def persist_proposals(patient, *, actor, expected_fingerprint, operation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        material = _locked_input(access, expected_fingerprint)
        return _persist_locked(access, material, _calculate(material), operation)


def decide_proposal(patient, proposal_id, *, actor, expected_fingerprint, action, expected_revision,
                    checked_original, operation_id, changes=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        material = _locked_input(access, expected_fingerprint)
        proposals = _calculate(material)
        selected = next((row for row in proposals["cycles"] if row["id"] == proposal_id), None)
        if selected is None:
            raise PermissionDenied("周期提议不可用或来源已变化。")
        _persist_locked(access, material, proposals, operation)
        cycle = TreatmentCycle.objects.get(patient=access.patient, source_key=selected["source_key"])
        tokens = {str(link.event_id): event_token(effective_event(link.event)) for link in cycle.event_links.select_related("event")}
        return revise_cycle(access.patient, cycle.pk, actor=access.actor, action=action, expected_revision=expected_revision,
                            checked_original=checked_original, operation_id=operation, changes=changes, expected_sources=tokens)
