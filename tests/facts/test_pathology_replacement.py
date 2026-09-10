from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import report_material
from apps.facts.models import Fact
from apps.facts.readmodels import digest, effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from tests.facts.pathology_factories import confirm_graph, ihc_fixture, review


pytestmark = pytest.mark.django_db


def replacement_request(patient, report, fields):
    return {"expected_material": digest(report_material(patient, report_ids=[report.pk])), "replacements": [
        {"old_fact_id": str(fields[key].pk), "value": deepcopy(fields[key].automatic_content["value"]),
         "fragments": [{"page_number": 1, "raw_text": fields[key].raw_text}],
         "bindings": deepcopy(fields[key].automatic_content["entity_context"]["bindings"])}
        for key in ["marker", "tps", "cps"]]}


def replace(patient, fields, changes):
    seed = Fact.objects.get(pk=fields["tps"].pk)
    return revise_fact(patient, seed.pk, actor=patient.account, action="REPLACE_CONTEXT", checked_original=True,
                       expected_revision=seed.revision_number, expected_source=effective_fact(seed)["current_source_token"], changes=changes)


def undo(patient, report, fields):
    seed = Fact.objects.get(pk=fields["tps"].pk)
    return revise_fact(patient, seed.pk, actor=patient.account, action="UNDO_CONTEXT", expected_revision=seed.revision_number,
                       expected_source=effective_fact(seed)["current_source_token"],
                       changes={"expected_material": digest(report_material(patient, report_ids=[report.pk]))})


def test_group_context_replacement_and_undo_preserve_old_sources_and_require_new_review(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "replace-context")
    confirm_graph(patient, fields)
    originals = {key: deepcopy(fields[key].automatic_content) for key in ["marker", "tps", "cps"]}
    old_ids = {str(f.pk) for f in fields.values()}
    result = replace(patient, fields, replacement_request(patient, report, fields))
    replacements = list(report.fields.exclude(pk__in=old_ids))
    assert len(replacements) == 3
    assert len({f.entity_key for f in replacements}) == 1 and replacements[0].entity_key != "ihc:a"
    assert all(effective_fact(f)["status"] == "PENDING" and not effective_fact(f)["usable"] for f in replacements)
    assert {str(f.pk) for f in replacements} == set(result.after["context_replacement"]["replacement_fact_ids"])
    for key, original in originals.items():
        fields[key].refresh_from_db()
        assert fields[key].automatic_content == original
        assert effective_fact(fields[key])["status"] == "EXCLUDED"
        with pytest.raises(ValidationError):
            review(patient, fields[key], "UNDO")
    undo(patient, report, fields)
    for key in originals:
        assert effective_fact(Fact.objects.get(pk=fields[key].pk))["status"] == "PENDING"
    assert all(effective_fact(Fact.objects.get(pk=f.pk))["status"] == "EXCLUDED" for f in replacements)
    with pytest.raises(ValidationError):
        undo(patient, report, fields)


@pytest.mark.parametrize("problem", ["missing_member", "stale_unselected_context", "wrong_binding"])
def test_group_replacement_validates_complete_heads_and_rolls_back_all_new_fields(django_user_model, problem):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "replace-invalid-" + problem)
    request = replacement_request(patient, report, fields)
    if problem == "missing_member":
        request["replacements"].pop()
    elif problem == "stale_unselected_context":
        review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-NEW"}, "raw_value": "SYN-NEW"})
    else:
        request["replacements"][-1]["bindings"][0]["target_fact_id"] = str(fields["clone"].pk)
    count = Fact.objects.count()
    with pytest.raises((ValidationError, FactConflict)):
        replace(patient, fields, request)
    assert Fact.objects.count() == count
    assert fields["tps"].revisions.count() == 0


def test_group_undo_refuses_reviewed_replacement_and_changed_unselected_original_context(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "replace-intervening")
    result = replace(patient, fields, replacement_request(patient, report, fields))
    replacement = Fact.objects.get(pk=result.after["context_replacement"]["replacement_fact_ids"][0])
    review(patient, replacement, "DEFER")
    with pytest.raises(FactConflict):
        undo(patient, report, fields)
    assert effective_fact(Fact.objects.get(pk=fields["tps"].pk))["status"] == "EXCLUDED"
