"""Authored synthetic original-page transcriptions; never private gold copies."""
from copy import deepcopy

from tests.documents.test_detail_viewer import _document, _patient


def score_value(kind="TPS", number="13", unit="%"):
    return {"score_kind": kind, "values": [number], "comparator": "EQ", "unit": unit,
            "unit_state": "PRINTED" if unit is not None else "NOT_PRINTED",
            "scale_kind": "SCORE" if kind == "CPS" else "PROPORTION", "approximate": False,
            "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": f"{kind} {number}{unit or ''}"}


def context_for(report, targets):
    return {"context_version": "IHC_CONTEXT_V1", "report_id": str(report.pk),
            "membership_policy": "IHC_CONTEXT_V1", "bindings": [
                {"role": role, "state": "BOUND" if target else "UNKNOWN",
                 "target_fact_id": str(target.pk) if target else None,
                 "target_entity_key": target.entity_key if target else None,
                 "proof_fragment_ordinals": [0] if target else [], "reason": None if target else "NOT_STATED"}
                for role, target in targets.items()]}


def report_fixture(django_user_model, name="pathology-core"):
    from apps.facts.clinical_services import create_manual_report

    client, patient = _patient(django_user_model, name)
    document, _ = _document(patient, page_count=1, status="PROCESSING_FAILED")
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk,
                                  title="合成病理与IHC报告", spans=[{"page_number": 1}],
                                  expected_lifecycle_revision=document.lifecycle_revision,
                                  expected_version_id=None, routing_kind="PATHOLOGY")
    return client, patient, document, report


def add_field(patient, report, key, entity, value, targets, *, raw=None, actor=None, context=None, source_role="CURRENT_RESULT"):
    from apps.facts.clinical_services import add_manual_clinical_field
    from apps.facts.clinical_readmodels import report_source_token

    # The test author explicitly transcribes the same synthetic specimen/assay
    # labels in this field's page evidence. No OCR coordinates are fabricated.
    raw = raw or "标本甲；检测甲；PD-L1；" + str(value.get("raw") or value.get("text") or value.get("value"))
    return add_manual_clinical_field(patient, actor=actor or patient.account, report_id=report.pk,
                                    entity_key=entity, field_key=key, value=deepcopy(value),
                                    fragments=[{"page_number": 1, "raw_text": raw}],
                                    expected_report_source=report_source_token(report),
                                    entity_context=context or context_for(report, targets), source_role=source_role)


def ihc_fixture(django_user_model, name="ihc-core"):
    client, patient, document, report = report_fixture(django_user_model, name)
    specimen = add_field(patient, report, "specimen.identity", "specimen:a", {"label": "标本甲", "raw": "标本甲"}, {})
    assay = add_field(patient, report, "assay.identity", "assay:a", {"label": "检测甲", "raw": "检测甲"}, {"SPECIMEN": specimen})
    targets = {"SPECIMEN": specimen, "ASSAY": assay}
    clone = add_field(patient, report, "assay.antibody", "assay:a", {"text": "SYN-CLONE-A"}, targets)
    marker = add_field(patient, report, "ihc.marker", "ihc:a", {"code": "PD_L1", "label": "PD-L1", "raw": "PD-L1"}, targets)
    targets["MARKER"] = marker
    tps = add_field(patient, report, "ihc.score", "ihc:a", score_value(), targets)
    cps = add_field(patient, report, "ihc.score", "ihc:a", score_value("CPS", "21", None), targets)
    return client, patient, document, report, {"specimen": specimen, "assay": assay, "clone": clone, "marker": marker, "tps": tps, "cps": cps}


def review(patient, fact, action="CONFIRM", changes=None, **kwargs):
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    fact.refresh_from_db()
    return revise_fact(patient, fact.pk, actor=kwargs.pop("actor", patient.account), action=action,
                       expected_revision=fact.revision_number, expected_source=effective_fact(fact)["current_source_token"],
                       checked_original=True, changes=changes, **kwargs)


def confirm_graph(patient, fields):
    for key in ["specimen", "assay", "clone", "marker", "tps", "cps"]:
        review(patient, fields[key])
