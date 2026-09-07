"""Live source checks shared by treatment decisions, views and derived outputs."""

from django.core.exceptions import PermissionDenied
from django.db import connection

from apps.documents.models import Document, UploadBatch
from apps.facts.readmodels import digest, effective_fact


def lock_source_documents(patient, document_ids):
    if not connection.in_atomic_block:
        raise RuntimeError("Treatment source locks require an atomic patient guard")
    requested = {str(value) for value in document_ids}
    query = Document.objects.filter(patient=patient, pk__in=requested)
    identities = list(query.values("pk", "batch_id"))
    if len(identities) != len(requested):
        raise PermissionDenied("治疗来源不可用。")
    tuple(UploadBatch.objects.select_for_update().filter(pk__in={r["batch_id"] for r in identities}).order_by("pk"))
    documents = tuple(query.select_for_update().order_by("pk"))
    if len(documents) != len(requested) or any(d.deleted_at is not None for d in documents):
        raise PermissionDenied("治疗来源不可用。")
    return documents


def evidence_state(evidence):
    document = evidence.document
    valid = (document.patient_id == evidence.event.patient_id and document.deleted_at is None
             and document.lifecycle_revision == evidence.lifecycle_revision
             and document.material_revision == evidence.material_revision
             and evidence.document_page.document_id == document.pk)
    current_token = ""
    if evidence.fact_id:
        state = effective_fact(evidence.fact)
        current_token = state["current_source_token"]
        valid = (valid and evidence.fact.document_id == document.pk and state["source_valid"]
                 and not state["historical"] and state["status"] not in {"DEFERRED", "EXCLUDED"}
                 and evidence.fact.revision_number == evidence.source_revision)
        text = evidence.fact.raw_text
    elif evidence.source_evidence_id:
        source = evidence.source_evidence
        current_token = digest({"text": source.source_text, "polygon": source.polygon,
                                "version": str(source.parsing_version_id), "page": str(source.document_page_id)})
        valid = (valid and source.parsing_version.active and source.parsing_version.document_id == document.pk
                 and source.document_page_id == evidence.document_page_id)
        text = source.source_text
    else:
        valid, text = False, ""
    valid = (valid and current_token == evidence.source_token
             and text[evidence.start_offset:evidence.end_offset] == evidence.raw_text)
    return {"valid": bool(valid), "current_token": current_token,
            "reason": "" if valid else "source_changed_or_unavailable"}


def event_sources(event):
    rows = []
    for evidence in event.evidence.select_related(
        "event", "document", "document_page", "fact__document__patient__account", "fact__document_page",
        "fact__parsing_version__document_summary", "fact__evidence", "source_evidence__parsing_version",
    ).order_by("document_id", "document_page_id", "start_offset", "pk"):
        state = evidence_state(evidence)
        rows.append({**evidence.source, "id": str(evidence.pk), "document_id": str(evidence.document_id),
                     "page": evidence.document_page.page_number, "fact_id": str(evidence.fact_id) if evidence.fact_id else None,
                     "source_token": evidence.source_token, "current_source_token": state["current_token"],
                     "source_valid": state["valid"], "reason": state["reason"],
                     "source_revision": evidence.source_revision, "lifecycle_revision": evidence.lifecycle_revision,
                     "material_revision": evidence.material_revision})
    return rows
