"""Preview is read-only; writes recompute current source identity under the guard."""
import uuid

import pytest
from django.core.exceptions import PermissionDenied

from apps.facts.revisions import add_manual_fact, revise_fact
from apps.patients.models import PatientMembership
from apps.treatments.models import TreatmentCycle, TreatmentDerivationRun, TreatmentEvent, TreatmentRevision
from apps.treatments.readmodels import treatment_material
from tests.documents.test_detail_viewer import _document, _patient

pytestmark = pytest.mark.django_db


def fact(patient, text="2024-03-01给予方案甲C1D1化疗。2024-03-22给予方案甲C2D1化疗。"):
    document, _ = _document(patient)
    return add_manual_fact(patient, document.pk, actor=patient.account, page_number=1,
                           category="TREATMENT", text=text)


def test_repeated_preview_creates_no_events_cycles_runs_or_revisions(django_user_model):
    from apps.treatments.derivations import proposal_preview
    _, patient = _patient(django_user_model, "derive-readonly")
    fact(patient)
    before = proposal_preview(patient, actor=patient.account)
    after = proposal_preview(patient, actor=patient.account)
    assert before == after and len(before["proposals"]["cycles"]) == 2
    assert not TreatmentEvent.objects.exists() and not TreatmentCycle.objects.exists()
    assert not TreatmentDerivationRun.objects.exists() and not TreatmentRevision.objects.exists()


def test_post_persists_one_current_baseline_and_actual_requester(django_user_model):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    _, patient = _patient(django_user_model, "derive-write")
    _, member = _patient(django_user_model, "derive-editor")
    PatientMembership.objects.create(patient=patient, account=member.account, role="EDITOR", revision=4)
    origin = fact(patient)
    preview = proposal_preview(patient, actor=member.account)
    run = persist_proposals(patient, actor=member.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    again = persist_proposals(patient, actor=member.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    assert run.pk == again.pk and run.requested_by_id == member.account_id and run.access_revision == 4
    assert TreatmentEvent.objects.count() == 2 and TreatmentCycle.objects.count() == 2
    assert all(e.origin == "AUTOMATIC" and e.revision_number == 0 for e in TreatmentEvent.objects.all())
    assert all(e.evidence.get().fact_id == origin.pk for e in TreatmentEvent.objects.all())


def test_source_changes_after_preview_cannot_persist_stale_proposal(django_user_model):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.services import TreatmentConflict
    _, patient = _patient(django_user_model, "derive-stale")
    origin = fact(patient)
    preview = proposal_preview(patient, actor=patient.account)
    revise_fact(patient, origin.pk, actor=patient.account, action="CORRECT", expected_revision=0,
                changes={"text": "2024-04-01给予方案乙化疗。"}, checked_original=True)
    with pytest.raises(TreatmentConflict):
        persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    assert not TreatmentDerivationRun.objects.exists()


def test_proposal_confirmation_preserves_automatic_baseline_and_fact_state(django_user_model):
    from apps.treatments.derivations import decide_proposal, proposal_preview
    from apps.facts.readmodels import effective_fact
    _, patient = _patient(django_user_model, "derive-confirm")
    origin = fact(patient)
    preview = proposal_preview(patient, actor=patient.account)
    proposal = preview["proposals"]["cycles"][0]
    result = decide_proposal(patient, proposal["id"], actor=patient.account, expected_fingerprint=preview["input_fingerprint"],
                             action="CONFIRM", expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
    result.cycle.refresh_from_db()
    assert result.cycle.initial_content["status"] == "PENDING"
    assert result.cycle.current_content["status"] == "CONFIRMED"
    assert result.author_id == patient.account_id and result.checked_original
    assert effective_fact(origin)["status"] == "PENDING"
    assert any(c["usable"] for c in treatment_material(patient, actor=patient.account)["cycles"])


def test_rejected_proposal_is_marked_decided_for_same_input_but_changed_source_is_new(django_user_model):
    from apps.treatments.derivations import decide_proposal, proposal_preview
    _, patient = _patient(django_user_model, "derive-rejection")
    origin = fact(patient)
    preview = proposal_preview(patient, actor=patient.account)
    proposal = preview["proposals"]["cycles"][0]
    decide_proposal(patient, proposal["id"], actor=patient.account, expected_fingerprint=preview["input_fingerprint"],
                    action="REJECT", expected_revision=0, checked_original=False, operation_id=uuid.uuid4())
    current = proposal_preview(patient, actor=patient.account)
    same = next(c for c in current["proposals"]["cycles"] if c["id"] == proposal["id"])
    assert same["decided"] and same["decision_status"] == "REJECTED"
    revise_fact(patient, origin.pk, actor=patient.account, action="CORRECT", expected_revision=0,
                changes={"text": "2024-04-01给予方案甲C1D1化疗。"}, checked_original=True)
    current = proposal_preview(patient, actor=patient.account)
    assert current["input_fingerprint"] != preview["input_fingerprint"]
    assert all(c["id"] != proposal["id"] and not c["decided"] for c in current["proposals"]["cycles"])


def test_corrected_fact_text_is_bound_to_its_revision_not_claimed_as_original_span(django_user_model):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    _, patient = _patient(django_user_model, "derive-corrected-source")
    origin = fact(patient, text="2024-03-01给予方案甲化疗。")
    revised = {"text": "2024-04-01给予方案乙C2D1化疗。"}
    revise_fact(patient, origin.pk, actor=patient.account, action="CORRECT", expected_revision=0, changes=revised, checked_original=True)
    preview = proposal_preview(patient, actor=patient.account)
    persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    event = TreatmentEvent.objects.get()
    evidence = event.evidence.get()
    assert evidence.source["text_basis"] == "CURRENT_FACT"
    assert evidence.source_revision == 1 and event.current_content["date"] == "2024-04-01"
    evidence.full_clean()
    assert treatment_material(patient, actor=patient.account)["events"][0]["source_valid"]


def test_read_only_and_revoked_members_cannot_persist_or_decide(django_user_model):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    _, patient = _patient(django_user_model, "derive-role-owner")
    _, member = _patient(django_user_model, "derive-role-reader")
    membership = PatientMembership.objects.create(patient=patient, account=member.account, role="VIEWER")
    fact(patient)
    preview = proposal_preview(patient, actor=member.account)
    with pytest.raises(PermissionDenied):
        persist_proposals(patient, actor=member.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    from django.utils import timezone
    membership.revoked_at = timezone.now()
    membership.save(update_fields=["revoked_at"])
    with pytest.raises(PermissionDenied):
        proposal_preview(patient, actor=member.account)


def test_new_organization_context_invalidates_old_confirmation_and_offers_new_input(django_user_model):
    from apps.treatments.derivations import decide_proposal, proposal_preview
    _, patient = _patient(django_user_model, "derive-new-context")
    fact(patient)
    before = proposal_preview(patient, actor=patient.account)
    item = before["proposals"]["cycles"][0]
    decision = decide_proposal(patient, item["id"], actor=patient.account, expected_fingerprint=before["input_fingerprint"],
        action="CONFIRM", expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
    fact(patient, text="2024-03-10暂停方案甲化疗。")
    after = proposal_preview(patient, actor=patient.account)
    assert all(c["id"] != item["id"] for c in after["proposals"]["cycles"])
    old = next(c for c in treatment_material(patient, actor=patient.account)["cycles"] if c["id"] == str(decision.cycle_id))
    assert old["status"] == "STALE" and not old["usable"]
    from apps.treatments.cycles import revise_cycle
    from apps.treatments.services import TreatmentConflict
    with pytest.raises(TreatmentConflict):
        revise_cycle(patient, decision.cycle_id, actor=patient.account, action="REVOKE", expected_revision=1, operation_id=uuid.uuid4())


def test_manual_event_and_explicit_event_correction_feed_fresh_automatic_proposals(django_user_model):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.services import revise_event
    from tests.treatments.test_manual_events import create
    _, patient = _patient(django_user_model, "derive-manual-input")
    manual = create(patient, patient.account, regimen_text="方案甲")
    before = proposal_preview(patient, actor=patient.account)
    assert len(before["proposals"]["cycles"]) == 1
    persist_proposals(patient, actor=patient.account, expected_fingerprint=before["input_fingerprint"], operation_id=uuid.uuid4())
    assert TreatmentEvent.objects.count() == 1
    revise_event(patient, manual.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                  changes={"date": "2024-03-05"}, checked_original=True, operation_id=uuid.uuid4())
    after = proposal_preview(patient, actor=patient.account)
    assert before["input_fingerprint"] != after["input_fingerprint"]
    assert after["proposals"]["cycles"][0]["content"]["anchor"] == "2024-03-05"
