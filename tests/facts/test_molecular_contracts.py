"""Synthetic value contracts only; no patients, reports, ORM or external knowledge."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib

from django.core.exceptions import ValidationError
import pytest


def contracts():
    return importlib.import_module("apps.facts.molecular_contracts")


def component(raw=None, state=None):
    return {"state": state or ("PRINTED" if raw is not None else "NOT_PRINTED"), "raw": raw}


def components(*values, state=None):
    return {"state": state or ("PRINTED" if values else "NOT_PRINTED"), "values": list(values)}


def variant(kind="SMALL_VARIANT"):
    common = {"kind": kind, "status": "COMPLETE", "scope": "SOMATIC", "raw": "SYN1 c.12+1G>A (p.?)"}
    if kind == "SMALL_VARIANT":
        return {**common, "gene": component("SYN1"), "expression": component("c.12+1G>A (p.?)"),
                "coding": components("c.12+1G>A"), "protein": components("p.?"), "codons": components("codon 4"),
                "transcripts": components("NM_SYN.2"), "locations": components("build-X chr2:12")}
    if kind == "COPY_NUMBER":
        return {**common, "raw": "SYN1 拷贝数增加", "gene": component("SYN1"), "change": component("拷贝数增加")}
    return {**common, "raw": "SYNB::SYNA", "expression": component("SYNB::SYNA"),
            "order_meaning": "FIVE_TO_THREE", "partners": [
                {"gene": component("SYNB"), "transcripts": components("NM_B.3"), "breakpoints": components("intron 2")},
                {"gene": component("SYNA"), "transcripts": components("NM_A.1"), "breakpoints": components("exon 5")}]}


def quantity(kind="ALLELE_FRACTION", **changes):
    value = {"status": "PARSED", "values": ["01.20"], "comparator": "EQ", "unit": "%", "unit_state": "PRINTED",
             "approximate": False, "measurement_kind": kind, "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": "01.20 %"}
    value.update(changes)
    return value


def panel(**changes):
    value = quantity("PANEL_SIZE", values=["0500"], unit="个", raw="约 0500 个基因", approximate=True)
    value["count_object"] = component("基因")
    value.update(changes)
    return value


def test_public_contract_is_independent_and_preserves_legacy_schema():
    from apps.facts import clinical_schema as legacy
    old_fields = dict(legacy.FIELDS)
    old = legacy.field_content("report.exam_date", {"value": "2026-09", "precision": "MONTH"}, "2026年9月")
    module = contracts()
    assert module.validate_value("assay.panel_name", {"text": " SYN panel  "}) == {"text": " SYN panel  "}
    assert legacy.FIELDS == old_fields
    assert legacy.field_content("report.exam_date", {"value": "2026-09", "precision": "MONTH"}, "2026年9月") == old
    assert "ihc.score" not in module.FIELDS and "pd_l1.score" not in module.FIELDS
    with pytest.raises(TypeError):
        module.FIELDS["assay.panel_name"] = None
    with pytest.raises(FrozenInstanceError):
        module.FIELDS["assay.panel_name"].label = "changed"


@pytest.mark.parametrize("key", ["assay.name", "assay.panel_name", "assay.molecular_method"])
def test_names_and_method_are_literal_not_external_normalization(key):
    value = {"text": "  SYN-NGS（原文）\n第二行 "}
    assert contracts().display_value(key, value) == value["text"]
    assert contracts().validate_value(key, value) is value


@pytest.mark.parametrize("kind", ["SMALL_VARIANT", "COPY_NUMBER", "FUSION"])
def test_tagged_variant_retains_complete_identity_and_does_not_mutate(kind):
    value = variant(kind)
    old = deepcopy(value)
    assert contracts().validate_value("variant.identity", value) is value
    assert contracts().display_value("variant.identity", value) == old["raw"]
    assert value == old


def test_same_gene_different_coding_transcript_and_partner_order_remain_distinct():
    first, second = variant(), variant()
    second["coding"] = components("c.12+2T>C")
    second["transcripts"] = components("NM_SYN.3")
    second["expression"] = component("c.12+2T>C (p.?)")
    second["raw"] = "SYN1 c.12+2T>C (p.?)"
    for value in (first, second):
        contracts().validate_value("variant.identity", value)
    assert first["coding"] != second["coding"] and first["transcripts"] != second["transcripts"]
    fusion = variant("FUSION")
    contracts().validate_value("variant.identity", fusion)
    assert [item["gene"]["raw"] for item in fusion["partners"]] == ["SYNB", "SYNA"]


@pytest.mark.parametrize("expression", ["c.12delinsTT", "p.Trp4*", "p.*9Glnext*3", "c.12=", "c.12-2A>G", "p.(Arg4?)"])
def test_variant_original_punctuation_is_not_rewritten(expression):
    value = component(expression)
    assert contracts().display_value("variant.expression", value) == expression
    assert value == component(expression)


def test_unresolved_identity_is_retained_but_cannot_claim_complete():
    value = variant()
    value["transcripts"] = components(state="UNKNOWN")
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", value)
    value["status"] = "INCOMPLETE"
    contracts().validate_value("variant.identity", value)
    assert value["transcripts"] == {"state": "UNKNOWN", "values": []}
    value["gene"] = component(None, "UNKNOWN")
    contracts().validate_value("variant.identity", value)


def test_codons_are_discriminating_original_components_and_unknown_is_incomplete():
    first, second = variant(), variant()
    second["codons"] = components("codon 5")
    for value in (first, second):
        contracts().validate_value("variant.identity", value)
    assert first["codons"]["values"] == ["codon 4"]
    assert second["codons"]["values"] == ["codon 5"]
    assert contracts().display_value("variant.codon", component("密码子 5")) == "密码子 5"
    second["codons"] = components(state="UNKNOWN")
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", second)
    second["status"] = "INCOMPLETE"
    contracts().validate_value("variant.identity", second)


def test_fusion_unknown_direction_and_scope_never_gains_a_biological_label():
    value = variant("FUSION")
    value.update(scope="UNKNOWN", order_meaning="AS_PRINTED_UNKNOWN")
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", value)
    value["status"] = "INCOMPLETE"
    contracts().validate_value("variant.identity", value)
    assert value["scope"] == "UNKNOWN" and value["order_meaning"] == "AS_PRINTED_UNKNOWN"
    assert [item["gene"]["raw"] for item in value["partners"]] == ["SYNB", "SYNA"]
    value = variant()
    value["scope"] = "UNKNOWN"
    contracts().validate_value("variant.identity", value)
    assert value["scope"] == "UNKNOWN"  # COMPLETE is structural, never somatic inference.


def test_unknown_quantity_and_equal_range_preserve_the_literal():
    value = quantity("TMB", status="UNRESOLVED", values=[], comparator=None, unit=None, unit_state="UNKNOWN", raw="TMB 数值不清")
    assert contracts().display_value("assay.tmb_value", value) == "TMB 数值不清"
    value = quantity("COPY_NUMBER", values=["2.0", "02.00"], comparator="RANGE", raw="2.0-02.00")
    assert contracts().display_value("variant.copy_number", value) == "2.0-02.00"


@pytest.mark.parametrize("field", ["gene", "expression"])
def test_gene_only_or_missing_gene_cannot_be_complete(field):
    value = variant()
    value[field] = component()
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", value)


@pytest.mark.parametrize("state", ["NOT_PRINTED", "UNKNOWN"])
def test_missing_components_stay_distinct_and_never_gain_text(state):
    value = component(None, state)
    contracts().validate_value("variant.transcript", value)
    assert contracts().display_value("variant.transcript", value) == ""
    assert value == {"state": state, "raw": None}
    value["raw"] = "guessed transcript"
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.transcript", value)


@pytest.mark.parametrize("key,kind", [("variant.allele_fraction", "ALLELE_FRACTION"), ("variant.copy_number", "COPY_NUMBER"),
                                     ("assay.msi_value", "MSI"), ("assay.tmb_value", "TMB")])
def test_numeric_fields_keep_original_quantity_and_missing_unit(key, kind):
    value = quantity(kind, unit=None, unit_state="NOT_PRINTED", values=["0", "123.450"], comparator="RANGE", raw="0 ～ 123.450")
    old = deepcopy(value)
    assert contracts().display_value(key, value) == "0 ～ 123.450"
    assert value == old
    value["measurement_kind"] = "TUMOR_PURITY"
    with pytest.raises(ValidationError):
        contracts().validate_value(key, value)


@pytest.mark.parametrize("comparator", ["EQ", "LT", "LE", "GT", "GE"])
def test_comparators_and_approximate_are_preserved(comparator):
    value = quantity(comparator=comparator, approximate=True)
    contracts().validate_value("variant.allele_fraction", value)
    assert value["comparator"] == comparator and value["approximate"] is True


@pytest.mark.parametrize("changes", [
    {"values": [True]}, {"values": ["NaN"]}, {"values": ["Infinity"]}, {"values": ["-1"]},
    {"values": [1]}, {"values": ["1e2"]}, {"values": ["１"]}, {"values": []},
    {"values": ["5", "2"], "comparator": "RANGE"}, {"values": ["1", "2"]},
    {"approximate": 1}, {"comparator": []}, {"unit": "", "unit_state": "PRINTED"},
    {"unit": "%", "unit_state": "NOT_PRINTED"}, {"status": "UNRESOLVED"},
    {"assertion": "POSITIVE"}, {"values": ["1" * 65]},
])
def test_numeric_malformed_and_inferred_values_raise_validation_error(changes):
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.allele_fraction", quantity(**changes))


def test_panel_integer_count_and_unparsed_original_never_become_zero():
    parsed = panel()
    contracts().validate_value("assay.panel_size", parsed)
    assert parsed["values"] == ["0500"]
    unparsed = panel(status="UNRESOLVED", values=[], comparator=None, raw="约数百个基因")
    assert contracts().display_value("assay.panel_size", unparsed) == "约数百个基因"
    assert unparsed["values"] == [] and unparsed["approximate"] is True
    with pytest.raises(ValidationError):
        contracts().validate_value("assay.panel_size", panel(values=["500.0"]))
    with pytest.raises(ValidationError):
        contracts().validate_value("assay.panel_size", {**unparsed, "values": ["0"]})


@pytest.mark.parametrize("key,value", [
    ("assay.collection_date", {"value": "2026", "precision": "YEAR", "raw": "2026年"}),
    ("assay.received_date", {"value": "2026-09", "precision": "MONTH", "raw": "2026年9月"}),
    ("assay.report_date", {"value": "2024-02-29", "precision": "DAY", "raw": "2024.02.29"}),
    ("assay.report_date", {"value": None, "precision": "UNKNOWN", "raw": "日期不清"}),
])
def test_independent_date_roles_preserve_precision_and_raw(key, value):
    assert contracts().display_value(key, value) == value["raw"]
    assert contracts().validate_value(key, value) is value


@pytest.mark.parametrize("value,precision", [("2026-02-29", "DAY"), ("2026-13", "MONTH"), (None, "DAY"),
                                           ("2026", "UNKNOWN"), (True, "YEAR"), ("2026", []), ("２０２６", "YEAR"),
                                           ("0000", "YEAR"), ("2026-00", "MONTH")])
def test_invalid_or_guessed_dates_raise_validation_error(value, precision):
    with pytest.raises(ValidationError):
        contracts().validate_value("assay.report_date", {"value": value, "precision": precision, "raw": "synthetic date"})


def test_msi_and_tmb_are_reported_separately_without_inference():
    module = contracts()
    category = {"code": "MSI_L", "raw": "MSI-L"}
    assert module.display_value("assay.msi_category", category) == "MSI-L"
    assert category["code"] == "MSI_L"
    module.validate_value("assay.msi_value", quantity("MSI", values=["0.00"], raw="0.00"))
    module.validate_value("assay.tmb_value", quantity("TMB", values=["99"], unit="mut/Mb", raw="99 mut/Mb"))
    assert module.display_value("assay.tmb_qualitative", {"code": "LOW", "raw": "报告记载低"}) == "报告记载低"
    with pytest.raises(ValidationError):
        module.validate_value("assay.msi_category", {"code": "MSS", "raw": "MSI-L", "derived_from_value": True})


def test_report_drug_combinations_and_grade_system_remain_original():
    module = contracts()
    drugs = {"names": ["药物乙", "药物甲"], "relation": "AND", "raw": "药物乙联合药物甲"}
    assert module.display_value("drug_evidence.drugs", drugs) == drugs["raw"]
    assert drugs["names"] == ["药物乙", "药物甲"]
    for relation in ("OR", "ALTERNATIVE", "UNKNOWN"):
        module.validate_value("drug_evidence.drugs", {**drugs, "relation": relation})
    module.validate_value("drug_evidence.direction", {"code": "UNCERTAIN", "raw": "未见耐药证据"})
    module.validate_value("drug_evidence.level", {"grade": component("Level-X"), "system": component(None, "UNKNOWN"), "raw": "Level-X"})
    assert module.display_value("drug_evidence.level", {"grade": component(), "system": component(), "raw": "未记载等级"}) == "未记载等级"


@pytest.mark.parametrize("key", ["variant.gene", "variant.coding", "variant.protein", "variant.codon", "variant.transcript",
                                  "variant.location", "variant.change", "variant.tier"])
def test_individually_selectable_components_retain_original_words(key):
    value = component(" SYN 原词.2（不可改写） ")
    assert contracts().display_value(key, value) == " SYN 原词.2（不可改写） "


@pytest.mark.parametrize("key", ["drug_evidence.statement", "drug_evidence.context"])
def test_report_drug_narrative_is_literal_not_a_treatment_event(key):
    value = {"text": "仅为报告原述：可能获益；本段不明示具体变异。"}
    assert contracts().display_value(key, value) == value["text"]


@pytest.mark.parametrize("value", [
    {"names": [], "relation": "UNKNOWN", "raw": "药物不清"},
    {"names": ["药甲", "药乙"], "relation": "SINGLE", "raw": "药甲、药乙"},
    {"names": ["药甲"], "relation": "AND", "raw": "药甲"},
    {"names": ["药甲", False], "relation": "UNKNOWN", "raw": "药甲"},
])
def test_incomplete_drug_groups_do_not_invent_combination(value):
    with pytest.raises(ValidationError):
        contracts().validate_value("drug_evidence.drugs", value)


@pytest.mark.parametrize("kind", ["SMALL_VARIANT", "COPY_NUMBER", "FUSION"])
def test_identity_wrong_tag_extra_and_missing_keys_rejected(kind):
    value = variant(kind)
    value["unproved_assay_id"] = "synthetic"
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", value)
    del value["unproved_assay_id"]
    del value["scope"]
    with pytest.raises(ValidationError):
        contracts().validate_value("variant.identity", value)


@pytest.mark.parametrize("key,value", [
    ([], {}), (None, {}), ("unknown", {}), ("variant.identity", []),
    ("variant.identity", {**variant(), "kind": []}), ("variant.identity", {**variant(), "scope": {}}),
    ("variant.identity", {**variant(), "gene": {"state": [], "raw": "SYN1"}}),
    ("variant.identity", {**variant(), "transcripts": {"state": "PRINTED", "values": [False]}}),
    ("variant.identity", {**variant("FUSION"), "partners": [None, {}]}),
    ("assay.name", {"text": True}), ("assay.panel_name", {"text": "x", "extra": True}),
    ("drug_evidence.direction", {"code": [], "raw": "synthetic"}),
    ("drug_evidence.level", {"grade": None, "system": {}, "raw": "x"}),
])
def test_bad_json_shapes_fail_as_validation_error_not_type_error(key, value):
    old = deepcopy(value)
    with pytest.raises(ValidationError):
        contracts().validate_value(key, value)
    assert value == old
