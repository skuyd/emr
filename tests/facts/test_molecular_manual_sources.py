import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import report_source_token, effective_field
from apps.facts.clinical_services import add_manual_clinical_field
from tests.facts.molecular_factories import graph, context_for
from tests.facts.test_molecular_contracts import quantity
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db


def create(patient, report, fields, texts, count, *, proof=0):
    context = context_for(report, {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    return add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, field_key="assay.tmb_value",
        entity_key="assay:a", value=quantity("TMB", unit="mut/Mb"), expected_report_source=report_source_token(report),
        entity_context=context, source_role="CURRENT_RESULT", fragments=[{"page_number": 1, "raw_text": text} for text in texts],
        **({"own_fragment_count": count} if count is not None else {}), reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [proof]})


@pytest.mark.parametrize("count", [None, 1, 2, 0, True, 3])
def test_manual_count_cannot_crop_leading_fragment_negation(django_user_model, count):
    _, patient, _, report, fields = graph(django_user_model)
    with pytest.raises(ValidationError):
        fact = create(patient, report, fields, ["标本甲；检测甲；12 mut/Mb；not", "negative"], count, proof=1)
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        assert effective_field(fact)["usable"]


def test_manual_count_cannot_drop_unassigned_trailing_modifier_or_reorder_it(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    with pytest.raises(ValidationError):
        create(patient, report, fields, ["标本甲；检测甲；12 mut/Mb；negative", "not established"], 1)


def test_complete_manual_window_remains_confirmable_and_retains_immutable_declaration(django_user_model):
    _, patient, _, report, fields = graph(django_user_model)
    fact = create(patient, report, fields, ["标本甲；检测甲；12 mut/Mb；negative"], 1)
    for item in [fields["specimen"], fields["assay"], fact]:
        review(patient, item)
    assert effective_field(fact)["usable"]
    assert fact.automatic_content["manual_source"] == {"version": "MOLECULAR_MANUAL_SOURCE_V1", "own_fragment_count": 1}
    review(patient, fact, "CORRECT", {"value": quantity("TMB", unit="mut/Mb"), "raw_value": fact.raw_text})
    assert effective_field(fact)["content"]["manual_source"] == fact.automatic_content["manual_source"]


@pytest.mark.parametrize("borrow_assertion", [False, True])
def test_real_copied_anchors_require_assertion_in_own_closed_statement(django_user_model, borrow_assertion):
    _, patient, _, report, fields = graph(django_user_model)
    context = context_for(report, {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    for binding in context["bindings"]:
        binding["proof_fragment_ordinals"] = [1]
    def create_and_confirm():
        fact = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk,
            field_key="assay.tmb_value", entity_key="assay:a", value=quantity("TMB", unit="mut/Mb"),
            expected_report_source=report_source_token(report), entity_context=context, source_role="CURRENT_RESULT",
            fragments=[{"page_number": 1, "raw_text": "12 mut/Mb；" if borrow_assertion else "12 mut/Mb；negative；"},
                       {"page_number": 1, "raw_text": "标本甲；检测甲；negative；"}], own_fragment_count=1,
            reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [1 if borrow_assertion else 0]})
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        return fact
    if borrow_assertion:
        with pytest.raises(ValidationError):
            create_and_confirm()
    else:
        assert effective_field(create_and_confirm())["usable"]


@pytest.mark.parametrize("route", ["reported_assertion", "negative_statement"])
def test_real_anchor_proof_cannot_hide_same_statement_modifier_past_own_count(django_user_model, route):
    _, patient, _, report, fields = graph(django_user_model)
    context = context_for(report, {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    for binding in context["bindings"]:
        binding["proof_fragment_ordinals"] = [1]
    value = quantity("TMB", unit="mut/Mb") if route == "reported_assertion" else {
        "text": "negative", "assertion": "NEGATIVE", "scope": {"state": "EXPLICIT", "raw": "small variants",
        "detection_kinds": [{"code": "SMALL_VARIANT", "raw": "small variants"}], "targets": [], "limitations": []}}
    with pytest.raises(ValidationError):
        fact = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk,
            field_key="assay.tmb_value" if route == "reported_assertion" else "assay.negative_statement", entity_key="assay:a", value=value,
            expected_report_source=report_source_token(report), entity_context=context, source_role="CURRENT_RESULT",
            fragments=[{"page_number": 1, "raw_text": "12 mut/Mb；small variants；negative"}, {"page_number": 1, "raw_text": "not 标本甲；检测甲"}], own_fragment_count=1,
            reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [0]} if route == "reported_assertion" else None)
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        assert effective_field(fact)["usable"]


@pytest.mark.parametrize("route", ["reported_assertion", "negative_statement"])
@pytest.mark.parametrize("negated", [False, True])
def test_all_proof_ordinals_still_require_complete_ordered_manual_statement(django_user_model, route, negated):
    _, patient, _, report, fields = graph(django_user_model)
    context = context_for(report, {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    value = quantity("TMB", unit="mut/Mb") if route == "reported_assertion" else {
        "text": "negative", "assertion": "NEGATIVE", "scope": {"state": "EXPLICIT", "raw": "small variants",
        "detection_kinds": [{"code": "SMALL_VARIANT", "raw": "small variants"}], "targets": ["SYN1"], "limitations": ["region A"]}}
    def create_and_confirm():
        fact = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk,
            field_key="assay.tmb_value" if route == "reported_assertion" else "assay.negative_statement", entity_key="assay:a", value=value,
            expected_report_source=report_source_token(report), entity_context=context, source_role="CURRENT_RESULT",
            fragments=[{"page_number": 1, "raw_text": text} for text in ["标本甲；检测甲；12 mut/Mb；", "not" if negated else "result:", "negative; small variants; SYN1; region A"]],
            own_fragment_count=3, reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [0, 1, 2]} if route == "reported_assertion" else None)
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        return fact
    if negated:
        with pytest.raises(ValidationError):
            fact = create_and_confirm()
            assert effective_field(fact)["usable"]
    else:
        assert effective_field(create_and_confirm())["usable"]
