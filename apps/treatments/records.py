"""Persistent, source-checked manual document/report/observation associations."""
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse

from apps.documents.models import Document
from apps.facts.clinical_readmodels import report_material, report_queryset
from apps.labs.readmodels import observation_queryset
from apps.labs.revisions import effective_observation
from apps.patients.access import authorize_patient

from .cycles import _checked, _check_revisions, _ids, _locked_cycles
from .models import CycleRecordLink
from .services import TreatmentConflict, _append_revision, _existing_operation
from .signals import digest
from .sources import lock_source_documents
from .validation import operation_uuid, validate_revision


KINDS = {"document": "document_id", "report": "report_id", "observation": "observation_id"}


def _document_identity(document):
    active = document.parsing_versions.filter(active=True).values("pk", "updated_at").first()
    return {"id": str(document.pk), "sha256": document.sha256, "lifecycle": document.lifecycle_revision,
            "material": document.material_revision, "active_version": active, "deleted": document.deleted_at}


def _assignment_token(patient, kind, identity):
    rows = CycleRecordLink.objects.filter(cycle__patient=patient, active=True, **{KINDS[kind]: identity}).order_by("pk")
    return digest(list(rows.values("pk", "cycle_id", "assigned", "source_token")))


def _record_state(patient, kind, identity):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValidationError("请选择资料、报告或检验记录。")
    identity = _ids([identity])[0]
    day, precision, available = None, "UNKNOWN", True
    if kind == "document":
        document = Document.objects.filter(patient=patient, pk=identity, deleted_at__isnull=True).first()
        if document is None:
            raise PermissionDenied("关联来源不可用。")
        current = document.parsing_versions.filter(active=True).first()
        summary = getattr(current, "document_summary", None)
        metadata = {"date": summary.document_date, "precision": summary.date_precision} if summary else {}
        token = digest({"document": _document_identity(document), "metadata": metadata})
        label = document.display_filename
        path = reverse("documents:document_summary", args=[document.pk])
        # A document with multiple reports does not inherit one arbitrary date.
        reports = report_material(patient, document_ids=[document.pk])
        if not reports and summary and summary.document_date:
            precision = summary.date_precision
            day = summary.document_date.isoformat()
            day = day[:4] if precision == "YEAR" else day[:7] if precision == "MONTH" else day
        token = digest({"base": token, "reports": reports})
        from apps.labs.readmodels import effective_rows
        from .laboratory import laboratory_source
        laboratory = [laboratory_source(row) for row in effective_rows(patient, version=current, include_uncertain=True)] if current else []
        if laboratory and not reports:
            dates = {(row["date"], row["date_precision"]) for row in laboratory}
            day, precision = next(iter(dates)) if len(dates) == 1 else (None, "UNKNOWN")
        token = digest({"base": token, "laboratory": laboratory})
    elif kind == "report":
        report = report_queryset().filter(document__patient=patient, document__deleted_at__isnull=True, pk=identity).first()
        if report is None:
            raise PermissionDenied("关联来源不可用。")
        document = report.document
        row = report_material(patient, report_ids=[report.pk], include_history=True)[0]
        available = row["source_valid"] and row["status"] != "EXCLUDED"
        token = digest({"document": _document_identity(document), "report": row})
        label, path = row["title"], reverse("facts:report", args=[report.pk])
        values = [field["content"]["value"] for field in row["fields"] if field["field_key"] == "report.exam_date" and field["usable"]]
        if len(values) == 1:
            value = values[0]
            day, precision = value.get("value"), value.get("precision", "UNKNOWN")
    else:
        original = observation_queryset().filter(pk=identity, parsing_version__document__patient=patient,
                                                 parsing_version__document__deleted_at__isnull=True).first()
        if original is None:
            raise PermissionDenied("关联来源不可用。")
        row = effective_observation(original)
        document = row.parsing_version.document
        from .laboratory import laboratory_source
        source = laboratory_source(row)
        available, token = source["source_valid"], source["source_token"]
        label = row.standard_name or row.raw_name
        path = reverse("labs:observation_source", args=[row.pk, "raw_value"])
        day, precision = source["date"], source["date_precision"]
    return {"kind": kind, "id": str(identity), "document_id": str(document.pk), "source_token": token,
            "date": day, "date_precision": precision, "source_valid": bool(available), "label": label, "url": path,
            "assignment_token": _assignment_token(patient, kind, identity)}


def record_state(patient, *, actor, kind, identity):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        row = _record_state(access.patient, kind, identity)
    authorize_patient(access.patient, actor, "read")
    return row


def assign_record(patient, cycle_id, *, actor, kind, identity, expected_source, expected_revision,
                  checked_original, operation_id, assigned, expected_assignment=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "write", lock=True)
        operation = operation_uuid(operation_id)
        identities = _ids([cycle_id])
        validate_revision(expected_revision)
        _checked(checked_original)
        if not isinstance(assigned, bool):
            raise ValidationError("请选择关联或保留未分组。")
        # Include the selected record and all cycle/regimen context before locking
        # any treatment child, using the repository's aggregate lock order.
        lock_source_documents(access.patient, Document.objects.filter(patient=access.patient, deleted_at__isnull=True).values_list("pk", flat=True))
        source = _record_state(access.patient, kind, identity)
        request_hash = digest({"action": "ASSIGN_RECORD", "cycle": str(identities[0]), "revision": expected_revision,
                               "kind": kind, "source": source["id"], "expected_source": expected_source,
                               "expected_assignment": expected_assignment, "assigned": assigned})
        previous = _existing_operation(access.patient, access.actor, operation, request_hash)
        if previous:
            return previous[0]
        if not source["source_valid"] or expected_source != source["source_token"]:
            raise TreatmentConflict("关联来源已变化，请刷新后核对。")
        if expected_assignment != source["assignment_token"]:
            raise TreatmentConflict("这份资料的周期归属已变化，请刷新后重新选择。")
        cycles, _, _, _ = _locked_cycles(access.patient, identities)
        cycle = cycles[0]
        _check_revisions(cycles, {str(cycle.pk): expected_revision})
        matches = {KINDS[kind]: source["id"]}
        CycleRecordLink.objects.filter(cycle__patient=access.patient, active=True, **matches).update(active=False)
        link = CycleRecordLink(cycle=cycle, origin="USER", source_token=source["source_token"], assigned=assigned, **matches)
        link.full_clean()
        link.save()
        before = deepcopy(cycle.current_content)
        after = {**deepcopy(before), "record_assignment": {"link_id": str(link.pk), "source_kind": kind,
                 "source_id": source["id"], "source_token": source["source_token"], "assigned": assigned}}
        return _append_revision(access, cycle, action="ASSIGN_RECORD", operation_id=operation, request_digest=request_hash,
                                before=before, after=after, checked_original=True, source_tokens={source["id"]: source["source_token"]})


def trusted_association_material(patient):
    rows = []
    for link in CycleRecordLink.objects.filter(cycle__patient=patient, active=True).select_related("cycle").order_by("pk"):
        kind = "document" if link.document_id else "report" if link.report_id else "observation"
        identity = getattr(link, KINDS[kind])
        try:
            source = _record_state(patient, kind, identity)
            valid = source["source_valid"] and source["source_token"] == link.source_token
        except PermissionDenied:
            valid = False
        rows.append({"id": str(link.pk), "cycle_id": str(link.cycle_id), "kind": kind, "source_id": str(identity),
                     "source_token": link.source_token, "source_valid": bool(valid), "origin": link.origin,
                     "assigned": link.assigned, "reason": "source_changed" if not valid else "" if link.assigned else "user_left_unassigned"})
    return rows


def association_material(patient, *, actor):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        rows = trusted_association_material(access.patient)
    authorize_patient(access.patient, actor, "read")
    return rows
