from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import report_material
from apps.facts.models import Fact
from apps.facts.pathology_services import affected_context_fields
from apps.facts.readmodels import digest, effective_fact
from apps.facts.revisions import revise_fact
from tests.facts.test_molecular_context import drug_graph, confirm

pytestmark = pytest.mark.django_db


def request(patient, report, seed):
    replacements = []
    for fact in affected_context_fields(seed, list(report.fields.all())):
        context = fact.automatic_content["entity_context"]
        replacements.append({"old_fact_id": str(fact.pk), "value": deepcopy(fact.automatic_content["value"]),
                             "fragments": [{"page_number": 1, "raw_text": fact.raw_text}],
                             "bindings": deepcopy(context["bindings"]), "association": deepcopy(context["association"]),
                             "reported_assertion": deepcopy(fact.automatic_content["reported_assertion"])})
    return {"expected_material": digest(report_material(patient, report_ids=[report.pk])), "replacements": replacements}


def action(patient, seed, name, changes):
    seed.refresh_from_db()
    return revise_fact(patient, seed.pk, actor=patient.account, action=name, expected_revision=seed.revision_number,
                       expected_source=effective_fact(seed)["current_source_token"], changes=changes, checked_original=True)


def test_replace_variant_includes_every_drug_user_and_preserves_second_variant(django_user_model):
    patient, _, report, fields, _, _, _ = drug_graph(django_user_model)
    confirm(patient, fields)
    seed = fields["identity"]
    changes = request(patient, report, seed)
    assert {entry["old_fact_id"] for entry in changes["replacements"]} == {str(fields[key].pk) for key in ("identity", "metric", "drugs")}
    old = {entry["old_fact_id"]: Fact.objects.get(pk=entry["old_fact_id"]).automatic_content for entry in changes["replacements"]}
    revision = action(patient, seed, "REPLACE_CONTEXT", changes)
    created = list(Fact.objects.filter(pk__in=revision.after["context_replacement"]["replacement_fact_ids"]))
    drugs = next(f for f in created if f.field_key == "drug_evidence.drugs")
    bindings = [b for b in drugs.automatic_content["entity_context"]["bindings"] if b["role"] == "VARIANT"]
    assert len(bindings) == 2 and bindings[0]["target_fact_id"] != str(seed.pk)
    assert bindings[1]["target_fact_id"] == str(fields["second"].pk)
    assert all(not effective_fact(f)["usable"] for f in created)
    for identity, content in old.items():
        fact = Fact.objects.get(pk=identity)
        assert fact.automatic_content == content and effective_fact(fact)["status"] == "EXCLUDED"
    action(patient, seed, "UNDO_CONTEXT", {"expected_material": digest(report_material(patient, report_ids=[report.pk]))})
    assert all(effective_fact(Fact.objects.get(pk=identity))["status"] == "PENDING" for identity in old)
    assert all(effective_fact(Fact.objects.get(pk=f.pk))["status"] == "EXCLUDED" for f in created)


@pytest.mark.parametrize("failure", ["missing_drug_user", "wrong_proof", "missing_association"])
def test_partial_replacement_rolls_back_every_new_candidate(django_user_model, failure):
    patient, _, report, fields, _, _, _ = drug_graph(django_user_model)
    changes = request(patient, report, fields["identity"])
    if failure == "missing_drug_user":
        changes["replacements"].pop()
    elif failure == "wrong_proof":
        changes["replacements"][-1]["fragments"] = [{"page_number": 1, "raw_text": "只有药物无任何关联依据"}]
    else:
        changes["replacements"][-1].pop("association")
    count = Fact.objects.count()
    with pytest.raises(ValidationError):
        action(patient, fields["identity"], "REPLACE_CONTEXT", changes)
    assert Fact.objects.count() == count
    assert not fields["identity"].revisions.exists()
