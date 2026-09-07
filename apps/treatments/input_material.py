"""Trusted current source read, called inside the authorized patient guard."""

from copy import deepcopy

from django.db.models import Q
from django.urls import reverse

from apps.documents.models import Document
from apps.facts.readmodels import effective_fact, fact_queryset
from apps.processing.material_review import material_state
from apps.processing.models import SourceEvidence

from .signals import RULE_VERSION, digest
from .sources import fact_state_token


def trusted_input_material(patient):
    from .models import TreatmentEvent
    from .readmodels import effective_event

    documents = list(Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("pk"))
    document_map = {document.pk: document for document in documents}
    document_states = {}
    for document in documents:
        current = document.parsing_versions.filter(active=True).first()
        state = material_state(document, current)
        document_states[str(document.pk)] = {"id": str(document.pk), "sha256": document.sha256,
                                            "lifecycle_revision": document.lifecycle_revision,
                                            "material_revision": document.material_revision,
                                            "material": state, "active_version": str(current.pk) if current else None}
    sources, fact_states = [], []
    facts = fact_queryset().filter(document__patient=patient, document__deleted_at__isnull=True, representation="EXCERPT")
    for fact in facts:
        state = effective_fact(fact)
        fact_states.append({"id": str(fact.pk), "token": fact_state_token(state)})
        if state["category"] != "TREATMENT":
            continue
        document = document_map[fact.document_id]
        material = document_states[str(document.pk)]["material"]
        eligible = (state["source_valid"] and not state["historical"] and state["status"] not in {"EXCLUDED", "DEFERRED"}
                    and state["source_token"] == state["current_source_token"]
                    and not (material["assessed"] and material["status"] == "NON_DOCUMENT"))
        current_text = state["content"]["text"]
        text_basis = "ORIGINAL_FACT" if current_text == fact.raw_text else "CURRENT_FACT"
        source = {**deepcopy(state["source"]), "fact_id": str(fact.pk), "source_evidence_id": None,
                  "text_basis": text_basis, "fact_state_token": fact_state_token(state)}
        if text_basis == "CURRENT_FACT":
            source["location_note"] = "提议来自当前人工更正文字；原件链接用于核对，不把更正后的文字范围称为原文。"
        sources.append({"id": "fact:" + str(fact.pk), "patient_id": str(patient.pk), "category": state["category"],
                        "source_kind": "FACT", "eligible": eligible, "status": state["status"], "text": current_text,
                        "text_basis": text_basis, "source_token": state["current_source_token"],
                        "source_revision": state["revision_number"], "source": source,
                        "lifecycle_revision": document.lifecycle_revision, "material_revision": document.material_revision,
                        "medication_order": "medication_order_transcription" in state["content"].get("limitations", [])})
    # Only bound metadata evidence may supply a hospital header. Arbitrary OCR
    # or another report's date cannot serve as an admission date.
    metadata = SourceEvidence.objects.filter(
        parsing_version__document__patient=patient, parsing_version__document__deleted_at__isnull=True,
        parsing_version__active=True, metadata_candidates__kind="DOCUMENT_DATE",
    ).select_related("parsing_version__document", "document_page").distinct().order_by("pk")
    for evidence in metadata:
        document = document_map[evidence.parsing_version.document_id]
        state = document_states[str(document.pk)]["material"]
        token = digest({"text": evidence.source_text, "polygon": evidence.polygon,
                        "version": str(evidence.parsing_version_id), "page": str(evidence.document_page_id)})
        source = {"document_id": str(document.pk), "page_id": str(evidence.document_page_id),
                  "page": evidence.document_page.page_number, "filename": document.display_filename,
                  "parsing_version": str(evidence.parsing_version_id), "source_evidence_id": str(evidence.pk), "fact_id": None,
                  "polygon": evidence.polygon, "location": "REGION" if evidence.polygon else "PAGE",
                  "url": reverse("documents:document_viewer", args=[document.pk]) + f"?page={evidence.document_page.page_number}&evidence={evidence.pk}"}
        sources.append({"id": "evidence:" + str(evidence.pk), "patient_id": str(patient.pk), "category": "TREATMENT",
                        "source_kind": "ADMISSION_EVIDENCE", "text_basis": "SOURCE_EVIDENCE", "status": "PENDING",
                        "source_token": token, "source_revision": 0, "text": evidence.source_text, "source": source,
                        "eligible": evidence.document_page.document_id == document.pk and not (state["assessed"] and state["status"] == "NON_DOCUMENT"),
                        "lifecycle_revision": document.lifecycle_revision, "material_revision": document.material_revision})
    event_decisions = [{**effective_event(event), "source_key": event.source_key}
                       for event in TreatmentEvent.objects.filter(patient=patient).filter(
                           Q(origin="USER") | Q(revision_number__gt=0)).order_by("pk")]
    material = {"rule_version": RULE_VERSION, "sources": sources, "fact_states": fact_states,
                "document_states": list(document_states.values()), "event_decisions": event_decisions}
    material["source_manifest_hash"] = digest([(d["id"], d["sha256"]) for d in material["document_states"]])
    material["fingerprint"] = digest(material)
    return material
