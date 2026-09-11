"""Application values keep the pure A0 contract and real context boundaries."""
from copy import deepcopy
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_schema import field_content, validate_content
from tests.facts.test_molecular_contracts import component, panel, quantity, variant


def context(roles, *, drug=False):
    return {"context_version": "MOLECULAR_CONTEXT_V1", "report_id": str(UUID(int=1)),
            "membership_policy": "MOLECULAR_CONTEXT_V1", "bindings": [
                {"role": role, "state": "UNKNOWN", "target_fact_id": None, "target_entity_key": None,
                 "proof_fragment_ordinals": [], "reason": "NOT_STATED"} for role in roles],
            "association": {"state": "UNKNOWN", "raw": None, "proof_fragment_ordinals": []} if drug else None}


SAMPLES = [
    ("assay.name", {"text": "SYN original assay"}),
    ("assay.panel_name", {"text": "SYN original panel"}),
    ("assay.molecular_method", {"text": "SYN sequencing"}),
    ("assay.panel_size", panel()),
    ("variant.identity", variant()),
    *[("variant." + key, component("SYN original " + key)) for key in
      ("gene", "expression", "coding", "protein", "codon", "transcript", "location", "change", "tier")],
    ("variant.allele_fraction", quantity()),
    ("variant.copy_number", quantity("COPY_NUMBER", values=["3.00"], unit=None, unit_state="NOT_PRINTED")),
    ("assay.msi_category", {"code": "MSI_L", "raw": "MSI-L"}),
    ("assay.msi_value", quantity("MSI")),
    ("assay.tmb_value", quantity("TMB", unit="mut/Mb")),
    ("assay.tmb_qualitative", {"code": "HIGH", "raw": "SYN high"}),
    ("drug_evidence.drugs", {"names": ["SYN-A", "SYN-B"], "relation": "OR", "raw": "SYN-A or SYN-B"}),
    ("drug_evidence.statement", {"text": "SYN report statement"}),
    ("drug_evidence.direction", {"code": "REPORT_RESISTANCE", "raw": "SYN resistance"}),
    ("drug_evidence.level", {"grade": component("II"), "system": component(state="UNKNOWN"), "raw": "II"}),
    ("drug_evidence.context", {"text": "SYN report context"}),
]


def roles_for(key):
    roles = ["SPECIMEN", "ASSAY"]
    if (key.startswith("variant.") and key != "variant.identity") or key.startswith("drug_evidence."):
        roles.append("VARIANT")
    if key.startswith("drug_evidence.") and key != "drug_evidence.drugs":
        roles.append("DRUG_EVIDENCE")
    return roles


@pytest.mark.parametrize("key,value", SAMPLES)
def test_application_registers_every_non_date_value_without_changing_original(key, value):
    original = deepcopy(value)
    content = field_content(key, value, "SYN original source", entity_context=context(roles_for(key), drug=key.startswith("drug_evidence.")),
                            source_role="REPORT_DRUG_EVIDENCE" if key.startswith("drug_evidence.") else "CURRENT_RESULT")
    validate_content(content, field_key=key)
    assert content["schema_version"] == "MOLECULAR_REPORT_V1" and content["category"] == "MOLECULAR"
    assert value == original and content["value"] == original
    assert content["reported_assertion"] == {"code": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": None, "proof_fragment_ordinals": []}


@pytest.mark.parametrize("key", ["assay.collection_date", "assay.received_date", "assay.report_date"])
@pytest.mark.parametrize("value,precision", [("2026", "YEAR"), ("2026-09", "MONTH"), ("2024-02-29", "DAY"), (None, "UNKNOWN")])
def test_a0_date_adapter_roundtrips_raw_without_changing_published_date_shape(key, value, precision):
    from apps.facts.molecular_adapters import molecular_field_content, molecular_value
    old_context = context(["SPECIMEN", "ASSAY"])
    old_context.pop("association")
    old_context.update(context_version="IHC_CONTEXT_V1", membership_policy="IHC_CONTEXT_V1")
    raw = "SYN source date phrase"
    content = molecular_field_content(key, {"value": value, "precision": precision, "raw": raw},
                                     entity_context=old_context, source_role="PRIMARY_ASSAY_METADATA")
    assert content["value"] == {"value": value, "precision": precision}
    assert content["raw_value"] == raw and content["date_raw"] == ""
    assert content["schema_version"] == "PATHOLOGY_IHC_V1" and content["category"] == "PATHOLOGY"
    assert molecular_value(key, content) == {"value": value, "precision": precision, "raw": raw}
    with pytest.raises(ValidationError):
        molecular_field_content(key, {"value": value, "precision": precision, "raw": raw}, raw_value="different source",
                                entity_context=old_context, source_role="PRIMARY_ASSAY_METADATA")


def negative():
    return {"text": "no copy-number change in the tested scope", "assertion": "NOT_DETECTED",
            "scope": {"state": "EXPLICIT", "raw": "SYN copy-number scope",
                      "detection_kinds": [{"code": "COPY_NUMBER", "raw": "SYN copy-number"}],
                      "targets": ["SYN1"], "limitations": []}}


def test_scoped_negative_does_not_replace_the_quantity_contract():
    content = field_content("assay.negative_statement", negative(), "SYN source", entity_context=context(["SPECIMEN", "ASSAY"]), source_role="CURRENT_RESULT")
    validate_content(content)
    assert content["value"]["scope"]["detection_kinds"] == [{"code": "COPY_NUMBER", "raw": "SYN copy-number"}]
    assert content["value"]["assertion"] == "NOT_DETECTED"
    from apps.facts.molecular_contracts import validate_value
    with pytest.raises(ValidationError):
        validate_value("assay.negative_statement", negative())


@pytest.mark.parametrize("change", [
    lambda v: v.update(assertion="NEGATIVE_FOR_ALL_GENES"),
    lambda v: v["scope"].update(state="UNKNOWN"),
    lambda v: v["scope"].update(targets=[True]),
    lambda v: v["scope"].update(detection_kinds=[{"code": "ALL", "raw": "all"}]),
    lambda v: v["scope"].update(detection_kinds=[]),
    lambda v: v["scope"].update(limitations=["x"] * 33),
    lambda v: v.update(extra="guessed"),
])
def test_scoped_negative_rejects_unknown_scope_with_invented_targets(change):
    value = negative()
    change(value)
    with pytest.raises(ValidationError):
        field_content("assay.negative_statement", value, "SYN source", entity_context=context(["SPECIMEN", "ASSAY"]), source_role="CURRENT_RESULT")


def drug_context():
    data = context(["SPECIMEN", "ASSAY"], drug=True)
    data["bindings"].extend([
        {"role": "VARIANT", "state": "BOUND", "target_fact_id": str(UUID(int=i)), "target_entity_key": "variant:" + str(i),
         "proof_fragment_ordinals": [i], "reason": None} for i in (2, 3)])
    data["association"] = {"state": "EXPLICIT", "raw": "SYN2 together with SYN3", "proof_fragment_ordinals": [2, 3]}
    return data


def test_drug_context_preserves_all_ordered_variant_targets():
    data = drug_context()
    content = field_content("drug_evidence.drugs", {"names": ["SYN-A"], "relation": "SINGLE", "raw": "SYN-A"}, "SYN source",
                            entity_context=data, source_role="REPORT_DRUG_EVIDENCE")
    assert [r["target_fact_id"] for r in content["entity_context"]["bindings"] if r["role"] == "VARIANT"] == [str(UUID(int=2)), str(UUID(int=3))]


@pytest.mark.parametrize("change", [
    lambda c: c.update(association=None),
    lambda c: c["bindings"].append(deepcopy(c["bindings"][-1])),
    lambda c: c["bindings"].append({"role": "VARIANT", "state": "UNKNOWN", "target_fact_id": None, "target_entity_key": None, "proof_fragment_ordinals": [], "reason": "NOT_STATED"}),
    lambda c: c["association"].update(proof_fragment_ordinals=[]),
    lambda c: c.update(context_version="IHC_CONTEXT_V1"),
])
def test_drug_context_rejects_partial_or_collapsing_associations(change):
    data = drug_context()
    change(data)
    with pytest.raises(ValidationError):
        field_content("drug_evidence.drugs", {"names": ["SYN-A"], "relation": "SINGLE", "raw": "SYN-A"}, "SYN source",
                      entity_context=data, source_role="REPORT_DRUG_EVIDENCE")
