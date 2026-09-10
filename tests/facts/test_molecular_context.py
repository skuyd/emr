from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import effective_field
from apps.facts.clinical_services import revise_report
from apps.facts.models import Fact
from tests.facts.molecular_factories import graph, add, context_for, variant_source
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_contracts import variant, quantity

pytestmark = pytest.mark.django_db


def confirm(patient, fields):
    for fact in fields.values():
        review(patient, fact)


def test_real_report_candidates_require_complete_reviewed_graph(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    assert report.schema_version == "MOLECULAR_REPORT_V1"
    assert not effective_field(fields["metric"])["usable"]
    confirm(patient, fields)
    row = effective_field(fields["metric"])
    assert row["usable"] and row["context_state"] == "RESOLVED"
    assert row["context_snapshot"]["context_version"] == "MOLECULAR_CONTEXT_V1"
    assert row["category_label"] == "分子/基因字段"
    assert {head["fact_id"] for head in row["context_snapshot"]["dependency_heads"]} == {
        str(fields[key].pk) for key in ("specimen", "assay", "identity")}
    review(patient, fields["assay"], "REVOKE")
    assert not effective_field(fields["metric"])["usable"]


def test_unknown_context_confirmed_text_is_not_usable(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    unknown = add(patient, report, "variant.allele_fraction", "variant:b", quantity(),
                  {"SPECIMEN": None, "ASSAY": None, "VARIANT": None})
    review(patient, unknown)
    row = effective_field(unknown)
    assert row["status"] == "CONFIRMED" and not row["usable"] and row["context_state"] == "UNLINKED"


@pytest.mark.parametrize("failure", ["wrong_entity", "foreign_report", "missing_proof", "assay_parent"])
def test_context_rejects_false_actual_associations(django_user_model, failure):
    _, patient, _, report, fields = graph(django_user_model)
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "VARIANT": fields["identity"]}
    context = context_for(report, targets)
    entity = "variant:a"
    raw = "标本甲；检测甲；" + variant_source() + "; 01.20 %"
    if failure == "wrong_entity":
        entity = "variant:other"
    elif failure == "foreign_report":
        _, _, _, _, foreign = graph(django_user_model, "foreign")
        context["bindings"][-1]["target_fact_id"] = str(foreign["identity"].pk)
    elif failure == "missing_proof":
        raw = "标本甲；检测甲；01.20 %"
    else:
        other = add(patient, report, "assay.identity", "assay:b", {"label": "检测乙", "raw": "检测乙"},
                    {"SPECIMEN": fields["specimen"]}, raw="标本甲；检测乙")
        context["bindings"][1].update(target_fact_id=str(other.pk), target_entity_key=other.entity_key)
        raw += "; 检测乙"
    count = Fact.objects.count()
    with pytest.raises(ValidationError):
        add(patient, report, "variant.allele_fraction", entity, quantity(), targets, context=context, raw=raw)
    assert Fact.objects.count() == count


def test_added_assay_metadata_invalidates_molecular_but_not_old_shared_identity(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    confirm(patient, fields)
    identity_token = effective_field(fields["assay"])["current_source_token"]
    metric_token = effective_field(fields["metric"])["current_source_token"]
    add(patient, report, "assay.panel_name", "assay:a", {"text": "SYN panel"},
        {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    assert effective_field(fields["assay"])["current_source_token"] == identity_token
    assert effective_field(fields["metric"])["current_source_token"] != metric_token
    assert not effective_field(fields["metric"])["usable"]


def test_duplicate_variant_anchor_rejected_and_identity_correction_requires_replacement(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    with pytest.raises(ValidationError):
        add(patient, report, "variant.identity", "variant:a", variant(),
            {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    changed = variant()
    changed["transcripts"]["values"] = ["NM_SYN.3"]
    with pytest.raises(ValidationError):
        review(patient, fields["identity"], "CORRECT", {"value": changed, "raw_value": changed["raw"]})


def test_report_exclusion_and_pending_only_undo(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    from apps.facts.clinical_readmodels import report_source_token
    confirm(patient, fields)
    for action in ("EXCLUDE", "UNDO"):
        report.refresh_from_db()
        revise_report(patient, actor=patient.account, report_id=report.pk, action=action,
                      expected_revision=report.revision_number, expected_source=report_source_token(report))
        assert not effective_field(fields["metric"])["usable"]
    assert effective_field(fields["metric"])["status"] == "PENDING"


@pytest.mark.parametrize("role", ["HISTORICAL", "CONTROL", "UNKNOWN"])
def test_non_current_source_never_usable(django_user_model, role):
    _, patient, _, report, fields = graph(django_user_model)
    # Source-role vocabulary uses the persisted contract, not prose aliases.
    from apps.facts.molecular_schema import SOURCE_ROLES
    role = {"HISTORICAL": "HISTORICAL_QUOTE"}.get(role, role)
    assert role in SOURCE_ROLES
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    value = add(patient, report, "assay.name", "assay:a", {"text": "SYN NGS"}, targets, role=role)
    confirm(patient, fields)
    review(patient, value)
    assert not effective_field(value)["usable"]


def drug_graph(model, name="molecular-drug"):
    _, patient, document, report, fields = graph(model, name)
    second = add(patient, report, "variant.identity", "variant:b", variant("FUSION"),
                 {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    fields["second"] = second
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "VARIANT": [fields["identity"], second]}
    raw = "标本甲；检测甲；" + variant_source() + " 与 " + variant_source("FUSION") + "：原文药物 SYN-A 与 SYN-B"
    context = context_for(report, targets, {"state": "EXPLICIT", "raw": raw, "proof_fragment_ordinals": [0]})
    drugs = add(patient, report, "drug_evidence.drugs", "drug_evidence:a",
                {"names": ["SYN-A", "SYN-B"], "relation": "AND", "raw": "SYN-A 与 SYN-B"},
                targets, raw=raw, context=context, role="REPORT_DRUG_EVIDENCE")
    fields["drugs"] = drugs
    return patient, document, report, fields, targets, context, raw


def test_drug_relation_keeps_both_actual_variant_heads_and_exclusion_of_either_invalidates(django_user_model):
    patient, _, _, fields, _, _, _ = drug_graph(django_user_model)
    confirm(patient, fields)
    row = effective_field(fields["drugs"])
    assert row["usable"]
    bindings = row["context_snapshot"]["bindings"]
    assert [b["target_fact_id"] for b in bindings if b["role"] == "VARIANT"] == [str(fields["identity"].pk), str(fields["second"].pk)]
    assert {str(fields["identity"].pk), str(fields["second"].pk)} <= {h["fact_id"] for h in row["context_snapshot"]["dependency_heads"]}
    review(patient, fields["second"], "EXCLUDE")
    assert not effective_field(fields["drugs"])["usable"]


@pytest.mark.parametrize("failure", ["set_incomplete_proof", "child_drops_variant", "child_reorders_variant", "own_drug_entity"])
def test_drug_association_rejects_partial_proof_or_parent_set(django_user_model, failure):
    patient, _, report, fields, targets, context, raw = drug_graph(django_user_model)
    value = {"code": "REPORT_RESISTANCE", "raw": "原报告耐药"}
    targets = {**targets, "DRUG_EVIDENCE": fields["drugs"]}
    context = context_for(report, targets, deepcopy(context["association"]))
    raw += "；原报告耐药"
    entity = "drug_evidence:a"
    if failure == "set_incomplete_proof":
        raw = raw.replace(variant("FUSION")["raw"], "缺失第二身份")
        context["association"]["raw"] = raw
    elif failure == "child_drops_variant":
        context["bindings"] = [b for b in context["bindings"] if b["target_fact_id"] != str(fields["second"].pk)]
    elif failure == "child_reorders_variant":
        context["bindings"][2:4] = reversed(context["bindings"][2:4])
    else:
        entity = "drug_evidence:wrong"
    with pytest.raises(ValidationError):
        add(patient, report, "drug_evidence.direction", entity, value, targets, raw=raw, context=context)


def test_reported_assertion_requires_actual_fragment_and_never_changes_numeric_contract(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "VARIANT": fields["identity"]}
    assertion = {"code": "DETECTED", "raw": "明确检出", "proof_fragment_ordinals": [0]}
    raw = "标本甲；检测甲；" + variant_source() + "；01.20 %"
    with pytest.raises(ValidationError):
        add(patient, report, "variant.allele_fraction", "variant:a", quantity(), targets, raw=raw, assertion=assertion)
    metric = add(patient, report, "variant.allele_fraction", "variant:a", quantity(), targets, raw=raw + "；明确检出", assertion=assertion)
    assert metric.automatic_content["value"]["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"
    assert metric.automatic_content["reported_assertion"] == assertion


def test_printed_identity_component_must_be_present_in_actual_source(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    fabricated = variant()
    fabricated["transcripts"]["values"] = ["NM_UNPRINTED.99"]
    with pytest.raises(ValidationError):
        add(patient, report, "variant.identity", "variant:wrong", fabricated,
            {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})


def test_conflicting_assay_members_cannot_qualify_derived_values(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    panels = [add(patient, report, "assay.panel_name", "assay:a", {"text": text}, targets) for text in ("SYN panel A", "SYN panel B")]
    review(patient, fields["specimen"])
    review(patient, fields["assay"])
    for panel in panels:
        review(patient, panel)
    review(patient, fields["identity"])
    review(patient, fields["metric"])
    assert not effective_field(fields["identity"])["usable"]
    assert not effective_field(fields["metric"])["usable"]


def test_exact_32_dependency_limit_never_truncates_33rd_member(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    for _ in range(29):
        add(patient, report, "assay.name", "assay:a", {"text": "SYN NGS"}, targets)
    from apps.facts.clinical_context import validate_context_candidate
    validate_context_candidate(fields["metric"])
    assert len(effective_field(fields["metric"])["context_snapshot"]["dependency_heads"]) == 32
    add(patient, report, "assay.name", "assay:a", {"text": "SYN NGS"}, targets)
    with pytest.raises(ValidationError):
        validate_context_candidate(fields["metric"])
    assert effective_field(fields["metric"])["context_state"] == "INVALID"
    assert not effective_field(fields["metric"])["usable"]


def test_invalid_persisted_context_fails_closed_without_changing_mode(django_user_model):
    _, _, _, _, fields = graph(django_user_model)
    content = deepcopy(fields["metric"].automatic_content)
    content.pop("entity_context")
    Fact.objects.filter(pk=fields["metric"].pk).update(automatic_content=content)
    fields["metric"].refresh_from_db()
    row = effective_field(fields["metric"])
    assert not row["usable"] and row["context_state"] == "INVALID"
    assert row["context_snapshot"]["context_version"] == "MOLECULAR_CONTEXT_V1"


def test_two_printed_transcript_components_of_same_identity_are_not_a_conflict(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    value = variant()
    value["transcripts"]["values"] = ["NM_SYN.2", "NM_SYN.3"]
    raw = "标本甲；检测甲；" + variant_source() + "；NM_SYN.3；01.20 %"
    parents = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    identity = add(patient, report, "variant.identity", "variant:multi", value, parents, raw=raw)
    targets = {**parents, "VARIANT": identity}
    components = [add(patient, report, "variant.transcript", identity.entity_key, {"state": "PRINTED", "raw": text}, targets, raw=raw)
                  for text in value["transcripts"]["values"]]
    metric = add(patient, report, "variant.allele_fraction", identity.entity_key, quantity(), targets, raw=raw)
    for fact in [fields["specimen"], fields["assay"], identity, *components, metric]:
        review(patient, fact)
    assert all(effective_field(f)["usable"] for f in components)
    assert effective_field(metric)["usable"]
    assert effective_field(identity)["content"]["value"]["transcripts"]["values"] == ["NM_SYN.2", "NM_SYN.3"]


@pytest.mark.parametrize("missing", ["scope", "kind", "target", "limitation"])
def test_negative_scope_cannot_expand_beyond_own_original_text(django_user_model, missing):
    _, patient, _, report, fields = graph(django_user_model)
    from tests.facts.test_molecular_schema import negative
    value = negative()
    value["scope"].update(targets=["SYN target"], limitations=["SYN limitation"])
    printed = [value["text"], value["scope"]["raw"], value["scope"]["detection_kinds"][0]["raw"], "SYN target", "SYN limitation"]
    positions = {"scope": 1, "kind": 2, "target": 3, "limitation": 4}
    printed.pop(positions[missing])
    raw = "标本甲；检测甲；" + "；".join(printed)
    # The fixture scope and kind must not accidentally be substrings of each
    # other; missing literal proof is the single controlled variation.
    if missing == "kind":
        value["scope"]["detection_kinds"][0]["raw"] = "SYN unprinted detection kind"
    with pytest.raises(ValidationError):
        add(patient, report, "assay.negative_statement", "assay:a", value,
            {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}, raw=raw)


def test_valid_scoped_negative_is_reviewable_but_correction_cannot_expand_scope(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    from tests.facts.test_molecular_schema import negative
    value = negative()
    raw = "标本甲；检测甲；" + value["text"] + "；" + value["scope"]["raw"] + "；" + value["scope"]["detection_kinds"][0]["raw"] + "；SYN1"
    fact = add(patient, report, "assay.negative_statement", "assay:a", value,
               {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}, raw=raw)
    for item in [fields["specimen"], fields["assay"], fact]:
        review(patient, item)
    assert effective_field(fact)["usable"]
    changed = deepcopy(value)
    changed["scope"]["targets"].append("SYN_UNPRINTED_TARGET")
    with pytest.raises(ValidationError):
        review(patient, fact, "CORRECT", {"value": changed, "raw_value": raw + "；SYN_UNPRINTED_TARGET"})
    assert fact.revisions.count() == 1


@pytest.mark.parametrize("case", ["opposite_detection_kind", "negative_assertion_positive_text", "reported_assertion_opposite_text"])
def test_finite_source_codes_cannot_contradict_printed_words(django_user_model, case):
    _, patient, _, report, fields = graph(django_user_model)
    parents = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    if case == "reported_assertion_opposite_text":
        key, value = "assay.tmb_value", quantity("TMB", unit="mut/Mb")
        raw = "标本甲；检测甲；01.20 mut/Mb；阳性"
        assertion = {"code": "NEGATIVE", "raw": "阳性", "proof_fragment_ordinals": [0]}
    else:
        key = "assay.negative_statement"
        text = "本范围未检出小变异" if case == "opposite_detection_kind" else "本范围明确检出拷贝数改变"
        kind_raw = "小变异" if case == "opposite_detection_kind" else "拷贝数"
        value = {"text": text, "assertion": "NOT_DETECTED", "scope": {"state": "EXPLICIT", "raw": "SYN panel范围",
                 "detection_kinds": [{"code": "COPY_NUMBER", "raw": kind_raw}], "targets": [], "limitations": []}}
        raw = "标本甲；检测甲；" + text + "；SYN panel范围；" + kind_raw
        assertion = None
    with pytest.raises(ValidationError):
        fact = add(patient, report, key, "assay:a", value, parents, raw=raw, assertion=assertion)
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        assert effective_field(fact)["usable"]
