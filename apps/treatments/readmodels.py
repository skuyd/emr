"""One current projection; an old confirmation cannot authorize changed sources."""

from copy import deepcopy

from django.db import transaction

from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .models import CycleLineage, TreatmentCycle, TreatmentEvent, TreatmentRegimen
from .sources import event_sources


def effective_event(event):
    sources = event_sources(event)
    source_valid = (event.origin == "USER" and not sources) or (bool(sources) and all(s["source_valid"] for s in sources))
    content = deepcopy(event.current_content)
    status = content["status"]
    if not source_valid:
        status = "STALE"
    return {"id": str(event.pk), "patient_id": str(event.patient_id), "origin": event.origin,
            "content": content, "status": status, "revision_number": event.revision_number,
            "source_valid": source_valid, "sources": sources, "rule_version": event.rule_version,
            "usable": status == "CONFIRMED" and source_valid,
            "reason": "" if source_valid else "来源已变化，请对照当前原件重新核对。"}


def event_token(row):
    return digest(row)


def _trusted_material(patient, *, include_history=False):
    events = [effective_event(event) for event in TreatmentEvent.objects.filter(patient=patient).order_by("created_at", "pk")]
    event_map = {row["id"]: row for row in events}
    regimen_rows = list(TreatmentRegimen.objects.filter(patient=patient).prefetch_related("events").order_by("created_at", "pk"))
    cycle_rows = list(TreatmentCycle.objects.filter(patient=patient).select_related("derivation_run").prefetch_related("event_links").order_by("created_at", "pk"))
    input_fingerprint = None
    if any(row.origin == "AUTOMATIC" for row in [*regimen_rows, *cycle_rows]):
        from .input_material import trusted_input_material
        input_fingerprint = trusted_input_material(patient)["fingerprint"]
    regimens = []
    for row in regimen_rows:
        tokens = row.current_content.get("event_tokens", {})
        source_ids = {str(event.pk) for event in row.events.all()}
        valid = ((row.origin == "USER" and not source_ids) or bool(source_ids)) and set(tokens) == source_ids
        valid = valid and all(key in event_map and event_map[key]["source_valid"] and event_token(event_map[key]) == value for key, value in tokens.items())
        if row.origin == "AUTOMATIC":
            valid = valid and row.current_content.get("input_fingerprint") == input_fingerprint
        status = row.current_content["status"] if valid else "STALE"
        regimens.append({"id": str(row.pk), "origin": row.origin, "revision_number": row.revision_number,
                         "content": deepcopy(row.current_content), "source_valid": bool(valid), "status": status,
                         "usable": bool(valid) and status == "CONFIRMED"})
    regimen_map = {row["id"]: row for row in regimens}
    cycles = []
    for cycle in cycle_rows:
        links = [{"event_id": str(link.event_id), "role": link.role, "source_token": link.source_token}
                 for link in cycle.event_links.all()]
        valid = bool(links) and all(link["event_id"] in event_map
                                  and event_map[link["event_id"]]["source_valid"]
                                  and event_token(event_map[link["event_id"]]) == link["source_token"] for link in links)
        if cycle.origin == "AUTOMATIC":
            valid = valid and cycle.derivation_run_id is not None and cycle.derivation_run.input_fingerprint == input_fingerprint
        if cycle.regimen_id:
            regimen = regimen_map.get(str(cycle.regimen_id))
            valid = valid and bool(regimen and regimen["source_valid"] and regimen["status"] not in {"REJECTED", "SUPERSEDED"})
            if regimen and cycle.current_content.get("regimen_token"):
                valid = valid and cycle.current_content["regimen_token"] == digest({"content": regimen["content"], "revision": regimen["revision_number"]})
        status = cycle.current_content["status"] if valid else "STALE"
        cycles.append({"id": str(cycle.pk), "regimen_id": str(cycle.regimen_id) if cycle.regimen_id else None,
                       "origin": cycle.origin, "revision_number": cycle.revision_number,
                       "content": deepcopy(cycle.current_content), "status": status, "source_valid": valid,
                       "event_links": links, "usable": valid and status == "CONFIRMED"})
    from .records import trusted_association_material
    material = {"events": events, "regimens": regimens, "cycles": cycles,
                "record_associations": trusted_association_material(patient),
                "lineage": [{"id": str(row.pk), "predecessor_id": str(row.predecessor_id),
                             "successor_id": str(row.successor_id), "operation_id": str(row.operation_id)}
                            for row in CycleLineage.objects.filter(predecessor__patient=patient).order_by("pk")]}
    if include_history:
        history = []
        for kind, model in [("event", TreatmentEvent), ("regimen", TreatmentRegimen), ("cycle", TreatmentCycle)]:
            for row in model.objects.filter(patient=patient).prefetch_related("revisions").order_by("pk"):
                history.append({"kind": kind, "id": str(row.pk), "initial_content": deepcopy(row.initial_content),
                    "revisions": [{"id": str(revision.pk), "sequence": revision.sequence, "action": revision.action,
                        "author_id": str(revision.author_id) if revision.author_id else None,
                        "created_at": revision.created_at.isoformat(), "operation_id": str(revision.operation_id),
                        "before": deepcopy(revision.before), "after": deepcopy(revision.after),
                        "checked_original": revision.checked_original, "source_tokens": deepcopy(revision.source_tokens)}
                        for revision in row.revisions.all()]})
        material["history"] = history
    material["fingerprint"] = digest(material)
    return material


def treatment_material(patient, *, actor, include_history=False):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        material = _trusted_material(access.patient, include_history=include_history)
    authorize_patient(access.patient, actor, "read")
    return material
