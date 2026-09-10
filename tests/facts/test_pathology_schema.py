from copy import deepcopy
import uuid

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_schema import field_content, validate_content
from tests.facts.pathology_factories import score_value


def authored_score_content():
    return {
        "category": "PATHOLOGY", "text": "原文IHC评分：TPS 13 %", "schema_version": "PATHOLOGY_IHC_V1",
        "field_key": "ihc.score", "value_type": "IHC_SCORE", "result_type": "SOURCE_REPORTED",
        "value": score_value(), "raw_value": "TPS 13%", "source_role": "CURRENT_RESULT",
        "entity_context": {"context_version": "IHC_CONTEXT_V1", "report_id": str(uuid.UUID(int=1)),
                           "membership_policy": "IHC_CONTEXT_V1", "bindings": [
                               {"role": role, "state": "UNKNOWN", "target_fact_id": None, "target_entity_key": None,
                                "proof_fragment_ordinals": [], "reason": "NOT_STATED"}
                               for role in ["SPECIMEN", "ASSAY", "MARKER"]]},
        "semantic_qualifiers": {},
    }


def test_new_score_schema_accepts_unlinked_source_text_without_inventing_unit_or_positivity():
    content = authored_score_content()
    validate_content(content)
    content["value"] = score_value("CPS", "21", None)
    content["text"] = "原文IHC评分：CPS 21（单位未印刷）"
    validate_content(content)
    assert content["value"]["unit"] is None
    assert content["value"]["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("entity_context"),
    lambda c: c["entity_context"]["bindings"].pop(),
    lambda c: c["entity_context"]["bindings"].append(deepcopy(c["entity_context"]["bindings"][0])),
    lambda c: c["entity_context"]["bindings"][0].update(state="BOUND"),
    lambda c: c["entity_context"].update(rank=40),
    lambda c: c["value"].update(values=[True]),
    lambda c: c["value"].update(values=["NaN"]),
    lambda c: c["value"].update(unit=None, unit_state="PRINTED"),
    lambda c: c["value"].update(score_kind="CPS", scale_kind="PROPORTION"),
    lambda c: c["value"].update(assertion="AUTOMATIC_POSITIVE"),
    lambda c: c["value"].update(comparator="RANGE", values=["8", "2"]),
    lambda c: c.update(source_role="PATIENT_BY_DEFAULT"),
])
def test_new_score_schema_rejects_ambiguous_or_invented_contract(mutate):
    content = authored_score_content()
    mutate(content)
    with pytest.raises(ValidationError):
        validate_content(content)


def test_old_field_schema_and_automatic_content_do_not_acquire_context_on_deploy():
    content = field_content("imaging.impression", {"text": "合成原结论"}, "合成原结论")
    assert content["schema_version"] == "1.0" and content["category"] == "IMAGING"
    assert "entity_context" not in content and "semantic_qualifiers" not in content
    validate_content(content)
