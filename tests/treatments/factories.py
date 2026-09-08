"""Synthetic persisted baselines; no extraction output or private corpus data."""

from copy import deepcopy
import uuid

from apps.facts.readmodels import effective_fact
from apps.facts.revisions import add_manual_fact
from apps.treatments.models import TreatmentEvent, TreatmentEvidence
from apps.treatments.signals import RULE_VERSION
from tests.documents.test_detail_viewer import _document


def source_event(patient, *, actor=None, text="2024-02-29 已给予合成方案 A 治疗。", document=None):
    if document is None:
        document, _ = _document(patient)
    fact = add_manual_fact(patient, document.pk, page_number=1, category="TREATMENT", text=text,
                           actor=actor or patient.account)
    state = effective_fact(fact)
    content = {"title": "合成方案 A", "kind": "SYSTEMIC_TREATMENT", "occurrence": "OCCURRED",
               "date": "2024-02-29", "date_precision": "DAY", "date_raw": "2024-02-29",
               "regimen_text": "合成方案 A", "cycle_ordinal": None, "cycle_day": None,
               "note": "", "status": "PENDING", "recorded_as": "SOURCE"}
    event = TreatmentEvent.objects.create(patient=patient, origin="AUTOMATIC", source_key=uuid.uuid4().hex,
                                          initial_content=deepcopy(content), current_content=deepcopy(content),
                                          rule_version=RULE_VERSION)
    evidence = TreatmentEvidence(event=event, document=document, document_page=fact.document_page,
                                  fact=fact, source_token=state["current_source_token"], source_revision=0,
                                  lifecycle_revision=document.lifecycle_revision, material_revision=document.material_revision,
                                  start_offset=0, end_offset=len(fact.raw_text), raw_text=fact.raw_text, source=state["source"])
    evidence.full_clean()
    evidence.save()
    return event, fact, document
