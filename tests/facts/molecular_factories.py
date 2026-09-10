"""Actual authorized services fed only authored synthetic source transcriptions."""
from copy import deepcopy

from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_schema import FIELDS
from apps.facts.clinical_services import add_manual_clinical_field, create_manual_report
from apps.facts.molecular_schema import SCHEMA, CONTEXT
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.pathology_factories import context_for as ihc_context
from tests.facts.test_molecular_contracts import variant, quantity


def variant_source(kind="SMALL_VARIANT"):
    return {"SMALL_VARIANT": "SYN1 c.12+1G>A (p.?)；codon 4；NM_SYN.2；build-X chr2:12",
            "COPY_NUMBER": "SYN1 拷贝数增加",
            "FUSION": "SYNB::SYNA；NM_B.3；intron 2；NM_A.1；exon 5"}[kind]


def report_fixture(model, name="molecular-core"):
    client, patient = _patient(model, name)
    document, _ = _document(patient, page_count=1, status="PROCESSING_FAILED")
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk,
                                  spans=[{"page_number": 1}], title="合成分子检测报告",
                                  expected_lifecycle_revision=document.lifecycle_revision,
                                  expected_version_id=None, routing_kind="MOLECULAR")
    return client, patient, document, report


def context_for(report, targets, association=None):
    rows = []
    for role, target in targets.items():
        for item in target if isinstance(target, list) else [target]:
            rows.append({"role": role, "state": "BOUND" if item else "UNKNOWN",
                         "target_fact_id": str(item.pk) if item else None,
                         "target_entity_key": item.entity_key if item else None,
                         "proof_fragment_ordinals": [0] if item else [], "reason": None if item else "NOT_STATED"})
    return {"context_version": CONTEXT, "report_id": str(report.pk), "membership_policy": CONTEXT,
            "bindings": rows, "association": association}


def add(patient, report, key, entity, value, targets, *, raw=None, role="CURRENT_RESULT", context=None, assertion=None):
    if raw is None and key == "variant.identity":
        raw = "标本甲；检测甲；" + variant_source(value["kind"])
    raw = raw or "标本甲；检测甲；" + str(value.get("raw") or value.get("text") or value.get("value"))
    if context is None:
        context = context_for(report, targets) if FIELDS[key].version == SCHEMA else ihc_context(report, targets)
    return add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key=entity,
                                    field_key=key, value=deepcopy(value), fragments=[{"page_number": 1, "raw_text": raw}],
                                    expected_report_source=report_source_token(report), entity_context=context,
                                    source_role=role, reported_assertion=assertion)


def graph(model, name="molecular-graph"):
    client, patient, document, report = report_fixture(model, name)
    specimen = add(patient, report, "specimen.identity", "specimen:a", {"label": "标本甲", "raw": "标本甲"}, {})
    assay = add(patient, report, "assay.identity", "assay:a", {"label": "检测甲", "raw": "检测甲"}, {"SPECIMEN": specimen})
    targets = {"SPECIMEN": specimen, "ASSAY": assay}
    identity = add(patient, report, "variant.identity", "variant:a", variant(), targets)
    metric = add(patient, report, "variant.allele_fraction", "variant:a", quantity(), {**targets, "VARIANT": identity},
                 raw="标本甲；检测甲；" + variant_source() + "; 01.20 %")
    return client, patient, document, report, {"specimen": specimen, "assay": assay, "identity": identity, "metric": metric}
