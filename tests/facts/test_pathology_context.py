from copy import deepcopy
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.facts.clinical_readmodels import report_material
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict
from tests.facts.pathology_factories import add_field, confirm_graph, context_for, ihc_fixture, report_fixture, review, score_value


pytestmark = pytest.mark.django_db


def test_real_manual_fields_keep_original_context_and_tps_cps_are_distinct_usable_slots(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model)
    original = deepcopy(fields["tps"].automatic_content)
    assert all(f.category == "PATHOLOGY" for f in fields.values())
    confirm_graph(patient, fields)
    rows = {f["id"]: f for f in report_material(patient, report_ids=[report.pk])[0]["fields"]}
    for key in ["tps", "cps"]:
        row = rows[str(fields[key].pk)]
        assert row["usable"] and row["context_state"] == "RESOLVED" and not row["conflict"]
        assert row["content"]["semantic_qualifiers"]["marker"]["label"] == "PD-L1"
        assert row["context_snapshot"]["membership"]
    assert rows[str(fields["cps"].pk)]["content"]["value"]["unit"] is None
    review(patient, fields["tps"], "CORRECT", {"value": score_value(number="17"), "raw_value": "TPS 17%"})
    fields["tps"].refresh_from_db()
    row = effective_fact(fields["tps"])
    assert row["content"]["value"]["values"] == ["17"] and row["usable"]
    assert row["content"]["entity_context"] == original["entity_context"]
    assert fields["tps"].automatic_content == original
    assert fields["tps"].revisions.last().before["context_snapshot"]


def test_unlinked_score_can_record_text_review_but_cannot_become_usable(django_user_model):
    _, patient, _, report = report_fixture(django_user_model, "ihc-unknown")
    field = add_field(patient, report, "ihc.score", "ihc:unknown", score_value(),
                      {"SPECIMEN": None, "ASSAY": None, "MARKER": None})
    review(patient, field)
    field.refresh_from_db()
    row = effective_fact(field)
    assert row["status"] == "CONFIRMED" and row["source_valid"]
    assert row["context_state"] == "UNLINKED" and not row["usable"]
    assert "未关联" in row["status_label"] and "未关联" in row["reason"]


def test_unselected_clone_change_and_undo_invalidate_saved_score_without_overwriting_old_confirmation(django_user_model):
    _, patient, _, _, fields = ihc_fixture(django_user_model, "ihc-clone-change")
    confirm_graph(patient, fields)
    fields["tps"].refresh_from_db()
    original_review = deepcopy(fields["tps"].revisions.last().after)
    prior_token = effective_fact(fields["tps"])["current_source_token"]
    review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CLONE-B"}, "raw_value": "SYN-CLONE-B"})
    row = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert row["status"] == "PENDING" and row["context_state"] == "STALE" and not row["usable"]
    changed = row["current_source_token"]
    assert changed != prior_token
    review(patient, fields["clone"], "UNDO")
    row = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert row["current_source_token"] not in {prior_token, changed}
    assert not row["usable"]
    assert fields["tps"].revisions.last().after == original_review


def test_new_unselected_context_member_changes_membership_and_requires_recheck(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "ihc-new-condition")
    confirm_graph(patient, fields)
    add_field(patient, report, "assay.method", "assay:a", {"code": "IHC", "raw": "IHC"},
              {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    assert not effective_fact(Fact.objects.get(pk=fields["tps"].pk))["usable"]


@pytest.mark.parametrize("problem", ["cross_report", "wrong_type", "missing_target", "contradictory_specimen", "invalid_proof"])
def test_common_manual_service_rejects_bad_context_and_rolls_back_whole_candidate(django_user_model, problem):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "bad-context-" + problem)
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "MARKER": fields["marker"]}
    context = context_for(report, targets)
    if problem == "cross_report":
        context["report_id"] = str(uuid.uuid4())
    elif problem == "wrong_type":
        context["bindings"][0].update(target_fact_id=str(fields["clone"].pk), target_entity_key=fields["clone"].entity_key)
    elif problem == "missing_target":
        context["bindings"][0]["target_fact_id"] = str(uuid.uuid4())
    elif problem == "contradictory_specimen":
        second = add_field(patient, report, "specimen.identity", "specimen:b", {"label": "标本乙", "raw": "标本乙"}, {})
        context["bindings"][0].update(target_fact_id=str(second.pk), target_entity_key=second.entity_key)
    else:
        context["bindings"][0]["proof_fragment_ordinals"] = [2]
    count = Fact.objects.count()
    with pytest.raises(ValidationError):
        add_field(patient, report, "ihc.score", "ihc:a", score_value(), targets, context=context)
    assert Fact.objects.count() == count


def test_anchor_db_uniqueness_survives_exclusion_but_general_results_are_not_deduplicated(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "anchor-unique")
    review(patient, fields["specimen"], "EXCLUDE")
    with pytest.raises((IntegrityError, ValidationError)):
        with transaction.atomic():
            original = fields["specimen"]
            Fact.objects.create(**{f.name: getattr(original, f.name) for f in Fact._meta.fields
                                   if f.name not in {"id", "revision_number", "created_at"}})
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "MARKER": fields["marker"]}
    # Excluded target is invalid for a new association, even when the caller
    # offers a fresh report token; the old anchor key is never reused.
    with pytest.raises(ValidationError):
        add_field(patient, report, "ihc.score", "ihc:a", score_value(number="8"), targets)


def test_correction_cannot_relabel_tps_as_cps_in_place(django_user_model):
    _, patient, _, _, fields = ihc_fixture(django_user_model, "score-slot")
    with pytest.raises(ValidationError):
        review(patient, fields["tps"], "CORRECT", {"value": score_value("CPS", "21", None), "raw_value": "CPS 21"})
    fields["tps"].refresh_from_db()
    assert fields["tps"].revision_number == 0


def test_reported_dates_keep_independent_roles_and_unknown_collection_date(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "ihc-date-roles")
    targets = {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}
    values = [("report_date", "2030-02-05", "DAY"), ("received_date", "2030-02-01", "DAY"), ("collection_date", None, "UNKNOWN")]
    for key, value, precision in values:
        field = add_field(patient, report, "assay." + key, "assay:a", {"value": value, "precision": precision}, targets)
        assert field.automatic_content["value"] == {"value": value, "precision": precision}
        assert {b["role"] for b in field.automatic_content["entity_context"]["bindings"]} == {"SPECIMEN", "ASSAY"}


def test_actual_collaborator_purge_changes_middle_dependency_author_identity(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    _, patient, _, _, fields = ihc_fixture(django_user_model, "ihc-dependency-author")
    _, collaborator = _patient(django_user_model, "ihc-dependency-author-other")
    PatientMembership.objects.create(patient=patient, account=collaborator.account, role="EDITOR")
    review(patient, fields["clone"], actor=collaborator.account)
    # A later owner review must not hide the erased middle historical author.
    confirm_graph(patient, fields)
    before = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert before["usable"]
    job = request_account_deletion(collaborator.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(job.pk).outcome == "PURGED"
    after = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert not after["usable"] and after["current_source_token"] != before["current_source_token"]
    assert fields["clone"].revisions.order_by("sequence").first().author_id is None


def test_dependency_budget_failure_is_visible_and_never_treated_as_a_truncated_valid_score(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "ihc-context-budget")
    for index in range(31):
        add_field(patient, report, "assay.antibody", "assay:a", {"text": f"SYN-{index}"},
                  {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    row = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert row["context_state"] == "INVALID" and not row["usable"]
    assert "过多" in row["reason"]
    with pytest.raises(ValidationError):
        review(patient, fields["tps"])


def test_nonpatient_control_role_is_explicit_even_after_text_review(django_user_model):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "ihc-control-role")
    confirm_graph(patient, fields)
    control = add_field(patient, report, "ihc.score", "ihc:a", score_value(number="99"),
                        {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"], "MARKER": fields["marker"]}, source_role="CONTROL")
    review(patient, control)
    row = effective_fact(Fact.objects.get(pk=control.pk))
    assert not row["usable"] and "非本次结果" in row["status_label"]
    assert row["reason"] and row["content"]["source_role"] == "CONTROL"


def test_correction_preserves_new_assertion_in_effective_qualifier_bundle(django_user_model):
    _, patient, _, _, fields = ihc_fixture(django_user_model, "ihc-corrected-assertion")
    confirm_graph(patient, fields)
    value = score_value()
    value.update(assertion="UNCERTAIN", raw="TPS约13%，表达不确定", approximate=True)
    review(patient, fields["tps"], "CORRECT", {"value": value, "raw_value": value["raw"]})
    row = effective_fact(Fact.objects.get(pk=fields["tps"].pk))
    assert row["content"]["semantic_qualifiers"]["qualitative_result"] == "UNCERTAIN"
    assert row["content"]["value"]["approximate"] is True
