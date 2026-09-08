import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.lesions.models import Lesion, LesionMatchProposal, LesionObservation, LesionOperation
from apps.lesions.readmodels import review_observations, review_proposals
from apps.lesions.services import decide_proposal, generate_proposals, undo_operation
from .factories import imaging_observation
from .test_relationships import expectations


pytestmark = pytest.mark.django_db


def pair(django_user_model, name):
    patient, _, _ = imaging_observation(django_user_model, name=name)
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    return patient


def arguments(proposal):
    return {"proposal_id": proposal.pk, "expected_revision": proposal.revision_number,
            "expected_fingerprint": proposal.fingerprint}


def test_generation_persists_pending_sources_and_repeat_preserves_rejection(django_user_model):
    patient = pair(django_user_model, "proposal-repeat")
    generated = generate_proposals(patient, actor=patient.account)
    assert len(generated) == 1
    proposal = LesionMatchProposal.objects.get()
    assert proposal.revisions.latest("sequence").after["status"] == "PENDING"
    assert proposal.revisions.get().operation.author_id == patient.account_id
    assert proposal.first_binding["report"]["id"] != proposal.second_binding["report"]["id"]
    assert Lesion.objects.count() == 0 and LesionObservation.objects.count() == 2
    assert all(not row["usable"] for row in review_observations(patient, actor=patient.account))
    original = proposal.fingerprint
    operation = decide_proposal(patient, actor=patient.account, action="REJECT", **arguments(proposal))
    assert operation.author_id == patient.account_id and not operation.checked_original
    assert operation.proposal_revisions.get().after["status"] == "REJECTED"
    generated_again = generate_proposals(patient, actor=patient.account)
    assert len(generated_again) == 1 and LesionMatchProposal.objects.count() == 1
    proposal.refresh_from_db()
    assert proposal.fingerprint == original and proposal.revisions.latest("sequence").after["status"] == "REJECTED"
    assert LesionOperation.objects.count() == 2
    undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    assert proposal.revisions.latest("sequence").after["status"] == "PENDING"


def test_accept_and_undo_include_both_observations_and_proposal_in_one_operation(django_user_model):
    patient = pair(django_user_model, "proposal-accept")
    generate_proposals(patient, actor=patient.account)
    assert LesionMatchProposal.objects.count() == 1
    proposal = LesionMatchProposal.objects.get()
    rows = review_observations(patient, actor=patient.account)
    operation = decide_proposal(patient, actor=patient.account, action="CONFIRM", **arguments(proposal),
                                expectations=expectations(rows), checked_original=True, name="观察 A")
    assert operation.proposal_revisions.get().after["status"] == "CONFIRMED"
    assert operation.observation_revisions.count() == 2 and operation.lesion_revisions.count() == 1
    assert all(row["usable"] for row in review_observations(patient, actor=patient.account))
    assert LesionOperation.objects.count() == 2
    reversal = undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    assert reversal.proposal_revisions.get().after["status"] == "PENDING"
    assert all(not row["usable"] and row["lesion_id"] is None for row in review_observations(patient, actor=patient.account))
    assert proposal.revisions.count() == 3 and Lesion.objects.count() == 1
    undo_operation(patient, actor=patient.account, operation_id=reversal.pk)
    current_proposal = review_proposals(patient, actor=patient.account)[0]
    assert current_proposal["status"] == "CONFIRMED" and current_proposal["source_current"]
    assert all(row["usable"] for row in review_observations(patient, actor=patient.account))


def test_sources_revised_and_restored_create_new_pending_proposal_without_reusing_old_decision(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    patient = pair(django_user_model, "proposal-source")
    generate_proposals(patient, actor=patient.account)
    assert LesionMatchProposal.objects.count() == 1
    original = LesionMatchProposal.objects.get()
    decide_proposal(patient, actor=patient.account, action="REJECT", **arguments(original))
    row = review_observations(patient, actor=patient.account)[0]
    field = Fact.objects.get(pk=next(field["id"] for field in row["fields"] if field["field_key"] == "lesion.site"))
    for action in ("REVOKE", "UNDO"):
        field.refresh_from_db()
        state = effective_fact(field)
        revise_fact(patient, field.pk, actor=patient.account, action=action, expected_revision=field.revision_number,
                    expected_source=state["current_source_token"], checked_original=True)
    original.refresh_from_db()
    assert review_proposals(patient, actor=patient.account) == []
    historical = review_proposals(patient, actor=patient.account, include_history=True)[0]
    assert historical["status"] == "STALE" and historical["decision"] == "REJECTED"
    with pytest.raises(ValidationError, match="来源"):
        decide_proposal(patient, actor=patient.account, action="CONFIRM", **arguments(original),
                        expectations=expectations(review_observations(patient, actor=patient.account)),
                        checked_original=True, name="观察 A")
    generate_proposals(patient, actor=patient.account)
    assert LesionMatchProposal.objects.count() == 2
    assert original.revisions.latest("sequence").after["status"] == "REJECTED"
    new = LesionMatchProposal.objects.exclude(pk=original.pk).get()
    assert new.fingerprint != original.fingerprint and new.revisions.get().after["status"] == "PENDING"
    assert Lesion.objects.count() == 0


def test_defer_does_not_assign_and_stale_revision_or_missing_original_check_cannot_confirm(django_user_model):
    patient = pair(django_user_model, "proposal-defer")
    generate_proposals(patient, actor=patient.account)
    assert LesionMatchProposal.objects.count() == 1
    proposal = LesionMatchProposal.objects.get()
    stale = arguments(proposal)
    operation = decide_proposal(patient, actor=patient.account, action="DEFER", **arguments(proposal))
    assert operation.proposal_revisions.get().after["status"] == "DEFERRED"
    with pytest.raises(ValidationError):
        decide_proposal(patient, actor=patient.account, action="REJECT", **stale)
    proposal.refresh_from_db()
    base = {**arguments(proposal), "expectations": expectations(review_observations(patient, actor=patient.account)),
            "name": "观察 A", "checked_original": True}
    for changes in ({"checked_original": False}, {"checked_original": "true"},
                    {"expected_revision": True}, {"expected_fingerprint": "old"}):
        with pytest.raises(ValidationError):
            decide_proposal(patient, actor=patient.account, action="CONFIRM", **{**base, **changes})
    assert Lesion.objects.count() == 0 and LesionOperation.objects.count() == 2


def test_proposal_generation_and_decisions_require_current_writer_and_patient_scope(django_user_model):
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    patient = pair(django_user_model, "proposal-permission")
    _, other = _patient(django_user_model, "proposal-stranger")
    with pytest.raises(PermissionDenied):
        generate_proposals(patient, actor=other.account)
    member = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    with pytest.raises(PermissionDenied):
        generate_proposals(patient, actor=other.account)
    member.role = "EDITOR"
    member.save(update_fields=["role"])
    generate_proposals(patient, actor=other.account)
    assert LesionMatchProposal.objects.count() == 1
    proposal = LesionMatchProposal.objects.get()
    assert proposal.revisions.get().operation.author_id == other.account_id
    with pytest.raises(PermissionDenied):
        decide_proposal(other, actor=other.account, action="REJECT", **arguments(proposal))
    member.role = "VIEWER"
    member.save(update_fields=["role"])
    with pytest.raises(PermissionDenied):
        decide_proposal(patient, actor=other.account, action="REJECT", **arguments(proposal))
    assert LesionOperation.objects.count() == 1
