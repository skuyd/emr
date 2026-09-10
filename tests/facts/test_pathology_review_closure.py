from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.models import Fact, FactRevision
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict
from tests.facts.pathology_factories import confirm_graph, ihc_fixture, review


pytestmark = pytest.mark.django_db


def report_action(patient, report, action):
    report.refresh_from_db()
    return revise_report(patient, actor=patient.account, report_id=report.pk, action=action,
                         expected_revision=report.revision_number, expected_source=report_source_token(report))


def test_parent_exclusion_restore_requires_fresh_review_and_preserves_originals(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-parent-undo")
    confirm_graph(patient, fields)
    originals = {key: deepcopy(field.automatic_content) for key, field in fields.items()}
    report_action(patient, report, "EXCLUDE")
    report_action(patient, report, "UNDO")
    for key, field in fields.items():
        field.refresh_from_db()
        state = effective_fact(field)
        assert state["status"] == "PENDING" and not state["usable"]
        assert field.automatic_content == originals[key]
    confirm_graph(patient, fields)
    assert effective_fact(Fact.objects.get(pk=fields["tps"].pk))["usable"]


def test_parent_restore_does_not_revive_previously_excluded_scores(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-excluded-child")
    confirm_graph(patient, fields)
    review(patient, fields["cps"], "EXCLUDE")
    report_action(patient, report, "EXCLUDE")
    report_action(patient, report, "UNDO")
    assert effective_fact(Fact.objects.get(pk=fields["cps"].pk))["status"] == "EXCLUDED"
    assert effective_fact(Fact.objects.get(pk=fields["tps"].pk))["status"] == "PENDING"


@pytest.mark.parametrize("change", ["source", "author"])
def test_parent_restore_checks_immutable_source_and_author_heads(django_user_model, change):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-restore-guard-" + change)
    confirm_graph(patient, fields)
    report_action(patient, report, "EXCLUDE")
    if change == "source":
        fragment = fields["marker"].source_fragments.get()
        # Simulate damaged persisted evidence. The normal model API itself
        # refuses edits to this immutable object.
        type(fragment).objects.filter(pk=fragment.pk).update(raw_text=fragment.raw_text + " changed source")
    else:
        # A database SET_NULL has precisely this shape after account erasure;
        # the existing actual-purge regression covers the lifecycle service.
        fields["clone"].revisions.filter(sequence=1).update(author=None)
    count = FactRevision.objects.count()
    with pytest.raises(FactConflict):
        report_action(patient, report, "UNDO")
    assert FactRevision.objects.count() == count
    assert effective_fact(Fact.objects.get(pk=fields["tps"].pk))["status"] == "EXCLUDED"


@pytest.mark.parametrize(("field_name", "attribute", "replacement"), [
    ("marker", "label", "Ki-67"), ("marker", "raw", "Ki-67"), ("marker", "code", "KI67"),
    ("specimen", "label", "标本乙"), ("specimen", "raw", "标本乙"),
    ("assay", "label", "检测乙"), ("assay", "raw", "检测乙"),
])
def test_any_anchor_identity_component_change_requires_group_replacement(django_user_model, field_name, attribute, replacement):
    _, patient, _, _, fields = ihc_fixture(django_user_model, "pathology-anchor-component-" + field_name + attribute)
    confirm_graph(patient, fields)
    field = fields[field_name]
    value = deepcopy(field.automatic_content["value"])
    value[attribute] = replacement
    count = FactRevision.objects.count()
    with pytest.raises(ValidationError):
        review(patient, field, "CORRECT", {"value": value, "raw_value": replacement})
    assert FactRevision.objects.count() == count
    assert effective_fact(Fact.objects.get(pk=fields["tps"].pk))["content"]["semantic_qualifiers"]["marker"]["label"] == "PD-L1"
