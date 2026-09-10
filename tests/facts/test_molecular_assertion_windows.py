"""A typed substring cannot discard modifiers from its actual source window."""
import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import effective_field
from tests.facts.molecular_factories import graph, add
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_contracts import quantity

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("route", ["reported_assertion", "negative_statement"])
@pytest.mark.parametrize("words", ["negative", "not negative", "not\nnegative"])
def test_actual_manual_assertion_uses_complete_source_window(django_user_model, route, words):
    _, patient, _, report, fields = graph(django_user_model)
    parents = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    raw = "标本甲；检测甲；01.20 mut/Mb；" + words + "; small variants; SYN1; region A"
    def create_and_confirm():
        if route == "reported_assertion":
            fact = add(patient, report, "assay.tmb_value", "assay:a", quantity("TMB", unit="mut/Mb"), parents,
                raw=raw, assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [0]})
        else:
            value = {"text": "negative", "assertion": "NEGATIVE", "scope": {"state": "EXPLICIT", "raw": "small variants",
                "detection_kinds": [{"code": "SMALL_VARIANT", "raw": "small variants"}], "targets": ["SYN1"], "limitations": ["region A"]}}
            fact = add(patient, report, "assay.negative_statement", "assay:a", value, parents, raw=raw)
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        return fact
    if words == "negative":
        assert effective_field(create_and_confirm())["usable"]
    else:
        with pytest.raises(ValidationError):
            fact = create_and_confirm()
            assert effective_field(fact)["usable"]


@pytest.mark.parametrize("words", ["detected", "none detected", "absence of detected variants"])
def test_actual_manual_unsupported_modifier_never_becomes_detected(django_user_model, words):
    _, patient, _, report, fields = graph(django_user_model)
    def create_and_confirm():
        fact = add(patient, report, "assay.tmb_value", "assay:a", quantity("TMB", unit="mut/Mb"),
            {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}, raw="标本甲；检测甲；01.20 mut/Mb；" + words,
            assertion={"code": "DETECTED", "raw": words, "proof_fragment_ordinals": [0]})
        for item in [fields["specimen"], fields["assay"], fact]:
            review(patient, item)
        return fact
    if words == "detected":
        assert effective_field(create_and_confirm())["usable"]
    else:
        with pytest.raises(ValidationError):
            fact = create_and_confirm()
            assert effective_field(fact)["usable"]
