"""One current projection; an old confirmation cannot authorize changed sources."""

from copy import deepcopy

from django.db import transaction

from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .models import TreatmentCycle, TreatmentEvent, TreatmentRegimen
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
    regimens = [{"id": str(row.pk), "origin": row.origin, "revision_number": row.revision_number,
                 "content": deepcopy(row.current_content)}
                for row in TreatmentRegimen.objects.filter(patient=patient).order_by("created_at", "pk")]
    cycles = []
    for cycle in TreatmentCycle.objects.filter(patient=patient).prefetch_related("event_links").order_by("created_at", "pk"):
        links = [{"event_id": str(link.event_id), "role": link.role, "source_token": link.source_token}
                 for link in cycle.event_links.all()]
        valid = bool(links) and all(link["event_id"] in event_map
                                  and event_map[link["event_id"]]["source_valid"]
                                  and event_token(event_map[link["event_id"]]) == link["source_token"] for link in links)
        status = cycle.current_content["status"] if valid else "STALE"
        cycles.append({"id": str(cycle.pk), "regimen_id": str(cycle.regimen_id) if cycle.regimen_id else None,
                       "origin": cycle.origin, "revision_number": cycle.revision_number,
                       "content": deepcopy(cycle.current_content), "status": status, "source_valid": valid,
                       "event_links": links, "usable": valid and status == "CONFIRMED"})
    material = {"events": events, "regimens": regimens, "cycles": cycles}
    material["fingerprint"] = digest(material)
    return material


def treatment_material(patient, *, actor, include_history=False):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        material = _trusted_material(access.patient, include_history=include_history)
    authorize_patient(access.patient, actor, "read")
    return material
