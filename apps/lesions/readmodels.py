"""Current report observations; source revisions never silently refresh links."""

from copy import deepcopy
import uuid

from apps.facts.clinical_readmodels import report_material
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .models import Lesion, LesionObservation


CONTEXT_KEYS = {"report.exam_date", "imaging.modality", "imaging.body_site",
                "comparison.statement", "comparison.reference_date"}


def observation_id(report_id, entity_key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"emr:lesion-observation:{report_id}:{entity_key}"))


def lesion_state(lesion):
    latest = lesion.revisions.order_by("-sequence").first()
    state = deepcopy(latest.after) if latest else {"name": lesion.original_name, "active": False}
    return {**state, "id": str(lesion.pk), "revision_number": lesion.revision_number,
            "operation_id": str(latest.operation_id) if latest else None}


def assignment_state(observation):
    latest = observation.revisions.order_by("-sequence").first() if observation else None
    return (deepcopy(latest.after) if latest else {
        "status": "UNASSIGNED", "lesion_id": None, "source_binding": None,
    })


def _one_usable(fields, key):
    candidates = [field for field in fields if field["field_key"] == key and field["usable"]]
    if not candidates or any(field.get("conflict") for field in candidates):
        return None
    values = [field["content"]["value"] for field in candidates]
    return deepcopy(values[0]) if all(value == values[0] for value in values) else None


def _source_binding(report, fields):
    return {
        "report": {key: report[key] for key in ("id", "revision_number", "revision_id",
                    "current_source_token", "source_valid", "status")},
        "fields": sorted([
            {**{key: field[key] for key in ("id", "revision_number", "revision_id", "current_source_token",
                                           "status", "source_valid", "usable", "conflict")},
             "content_digest": digest(field["content"])} for field in fields
        ], key=lambda field: field["id"]),
    }


def observation_material(patient, *, include_unavailable=False):
    """Trusted caller must authorize this patient before reading the material."""
    stored = {str(row.pk): row for row in LesionObservation.objects.filter(patient=patient).prefetch_related("revisions")}
    lesions = {str(row.pk): lesion_state(row) for row in Lesion.objects.filter(patient=patient).prefetch_related("revisions")}
    result = []
    for report in report_material(patient, include_history=include_unavailable):
        if report["routing_kind"] != "IMAGING":
            continue
        local = {}
        context = []
        for field in report["fields"]:
            if field["field_key"].startswith("lesion."):
                local.setdefault(field["entity_key"], []).append(field)
            elif field["field_key"] in CONTEXT_KEYS:
                context.append(field)
        for key, fields in local.items():
            site_fields = [field for field in fields if field["field_key"] == "lesion.site"]
            if not site_fields:
                continue
            identity = observation_id(report["id"], key)
            record = stored.pop(identity, None)
            binding = _source_binding(report, fields + context)
            assignment = assignment_state(record)
            lesion = lesions.get(assignment["lesion_id"])
            source_usable = bool(report["source_valid"] and report["status"] == "ACTIVE"
                                 and _one_usable(fields, "lesion.site"))
            stale = assignment["source_binding"] is not None and assignment["source_binding"] != binding
            usable = (source_usable and assignment["status"] == "CONFIRMED" and not stale
                      and bool(lesion and lesion["active"]))
            status = "STALE" if stale else assignment["status"]
            if not report["source_valid"] or report["status"] != "ACTIVE":
                status = "UNAVAILABLE"
            result.append({
                "id": identity, "patient_id": str(patient.pk), "report_id": report["id"],
                "document_id": report["document_id"], "entity_key": key,
                "report_title": report["title"], "pages": report["pages"],
                "fields": fields, "context_fields": context,
                "site": [field["content"]["value"]["text"] for field in site_fields],
                "laterality": _one_usable(fields, "lesion.laterality"),
                "date": _one_usable(context, "report.exam_date"),
                "method": _one_usable(context, "imaging.modality"),
                "body_site": _one_usable(context, "imaging.body_site"),
                "source_usable": source_usable, "source_binding": binding, "source_token": digest(binding),
                "assignment": assignment, "revision_number": record.revision_number if record else 0,
                "lesion_id": assignment["lesion_id"], "lesion_name": lesion["name"] if lesion else "",
                "usable": usable, "status": status,
            })
    if include_unavailable:
        for identity, record in stored.items():
            assignment = assignment_state(record)
            lesion = lesions.get(assignment["lesion_id"])
            result.append({
                "id": identity, "patient_id": str(patient.pk), "report_id": str(record.original_report_id),
                "document_id": str(record.document_id) if record.document_id else None, "entity_key": record.entity_key,
                "report_title": "来源报告不可用", "pages": [], "fields": [], "context_fields": [], "site": [],
                "laterality": None, "date": None, "method": None, "body_site": None,
                "source_usable": False, "source_binding": None, "source_token": None,
                "assignment": assignment, "revision_number": record.revision_number,
                "lesion_id": assignment["lesion_id"], "lesion_name": lesion["name"] if lesion else "",
                "usable": False, "status": "UNAVAILABLE",
            })
    return sorted(result, key=lambda row: ((row["date"] or {}).get("value") or "", row["id"]))


def review_observations(patient, *, actor, include_unavailable=False):
    access = authorize_patient(patient, actor)
    return observation_material(access.patient, include_unavailable=include_unavailable)
