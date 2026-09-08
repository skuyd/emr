"""Synthetic sentence scope survives source-bound automatic persistence."""
import uuid

import pytest

from apps.treatments.derivations import persist_proposals, proposal_preview
from apps.treatments.models import TreatmentCycle, TreatmentDerivationRun, TreatmentEvent
from apps.treatments.readmodels import effective_event
from tests.documents.test_detail_viewer import _patient
from tests.treatments.test_derivation_service import fact

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("text,states,cycles,governing_word", [
    ("以下均为计划：2024-01-01给予方案甲化疗、2024-01-08给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("计划于2024-01-01给予方案甲化疗及2024-01-08给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("计划于2024-01-01给予方案甲化疗，同时给予方案乙放疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("以下两次治疗均取消：2024-01-01给予方案甲化疗、2024-01-08给予方案乙化疗。", ["NEGATED", "NEGATED"], 0, "取消"),
    ("2024-01-01给予方案甲化疗，计划于2024-01-08给予方案乙化疗。", ["OCCURRED", "PLANNED"], 1, None),
    ("2024-01-01给予方案甲化疗，取消2024-01-08给予方案乙化疗。", ["OCCURRED", "NEGATED"], 1, None),
    ("计划于2024-01-01给予方案甲化疗，但2024-01-08实际给予方案乙化疗。", ["PLANNED", "OCCURRED"], 1, None),
    ("计划于2024-01-01给予方案甲化疗。2024-01-08给予方案乙化疗。", ["PLANNED", "OCCURRED"], 1, None),
    ("2024-01-01未行方案甲化疗；2024-01-08给予方案乙化疗。", ["NEGATED", "OCCURRED"], 1, None),
    ("2024-01-01给予方案甲化疗。2024-01-08给予方案乙化疗。", ["OCCURRED", "OCCURRED"], 2, None),
    ("2024-01-01计划给予方案甲化疗及2024-01-08给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("2024-01-01给予方案甲化疗、2024-01-08给予方案乙化疗，两次均取消。", ["NEGATED", "NEGATED"], 0, "取消"),
    ("2024-01-01给予方案甲化疗、2024-01-08给予方案乙化疗，以上均为计划。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("2024-01-01计划给予方案甲化疗，同时给予方案乙放疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("以下均为计划：2024-01-01按实际体重给予方案甲化疗、2024-01-08按实际体重给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("计划于2024-01-01给予方案甲化疗，随后2024-01-08给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("以下均为计划：2024-01-01给予方案甲化疗，最终2024-01-08给予方案乙化疗。", ["PLANNED", "PLANNED"], 0, "计划"),
    ("计划于2024-01-01给予方案甲化疗，但2024-01-08已经给予方案乙化疗。", ["PLANNED", "OCCURRED"], 1, None),
    ("计划于2024-01-01给予方案甲化疗，但实际于2024-01-08给予方案乙化疗。", ["PLANNED", "OCCURRED"], 1, None),
    ("计划于2024-01-01给予方案甲化疗，但已于2024-01-08给予方案乙化疗。", ["PLANNED", "OCCURRED"], 1, None),
])
def test_governing_and_local_modifiers_retain_their_actual_sources(django_user_model, text, states, cycles, governing_word):
    _, patient = _patient(django_user_model, "occurrence-permanent-" + uuid.uuid4().hex)
    origin = fact(patient, text=text)
    preview = proposal_preview(patient, actor=patient.account)
    persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    events = sorted(TreatmentEvent.objects.filter(patient=patient), key=lambda row: (row.current_content["date"] or "", row.current_content["regimen_text"]))
    assert len(events) == len(states)
    assert [event.current_content["occurrence"] for event in events] == states
    assert TreatmentCycle.objects.filter(patient=patient).count() == cycles
    for event in events:
        evidence = event.evidence.get()
        assert evidence.fact_id == origin.pk
        assert text[evidence.start_offset:evidence.end_offset] == evidence.raw_text
        if governing_word:
            assert governing_word in evidence.raw_text
        if basis := evidence.source.get("occurrence_basis"):
            assert evidence.start_offset <= basis["start_offset"] < basis["end_offset"] <= evidence.end_offset
            assert text[basis["start_offset"]:basis["end_offset"]] == basis["raw_text"]
            assert basis["occurrence"] == event.current_content["occurrence"]
    if states == ["OCCURRED", "PLANNED"]:
        assert "计划" not in events[0].evidence.get().raw_text
        assert "计划" in events[1].evidence.get().raw_text


def test_new_rule_does_not_reuse_an_old_run_or_present_its_events_as_current(django_user_model, monkeypatch):
    from apps.treatments import derivations, input_material, proposals, signals
    _, patient = _patient(django_user_model, "occurrence-rule-upgrade")
    fact(patient, text="2024-01-01给予方案甲化疗。")
    with monkeypatch.context() as old:
        for module in (signals, proposals, input_material, derivations):
            old.setattr(module, "RULE_VERSION", "treatment-proposals-1")
        preview = proposal_preview(patient, actor=patient.account)
        prior = persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
        original = TreatmentEvent.objects.get(patient=patient)
        assert effective_event(original)["source_valid"]
    assert not effective_event(original)["source_valid"]
    assert effective_event(original)["status"] == "STALE"
    preview = proposal_preview(patient, actor=patient.account)
    current = persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    assert current.pk != prior.pk
    assert TreatmentDerivationRun.objects.filter(patient=patient).count() == 2
    assert TreatmentEvent.objects.filter(pk=original.pk).exists()
    assert TreatmentEvent.objects.filter(patient=patient, rule_version=signals.RULE_VERSION).count() == 1


def test_rule_upgrade_invalidates_previously_confirmed_export_and_share(django_user_model, monkeypatch):
    from apps.exports import treatment
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import create_preview, get_preview
    from apps.patients.sharing import create_share, exchange_share_token
    from apps.treatments import derivations, input_material, proposals, signals
    from apps.treatments.services import revise_event
    from tests.exports.test_treatment_exports import selection
    from tests.patients.test_family_access import family

    owner, patient, _, _, _ = family(django_user_model, "occurrence-rule-output")
    assert owner.get("/visit/").status_code == 200
    origin = fact(patient, text="2024-01-01给予方案甲化疗。")
    reader, own = _patient(django_user_model, "occurrence-rule-recipient")
    reader.get("/shared/open/")
    with monkeypatch.context() as old:
        for module in (signals, proposals, input_material, derivations, treatment):
            old.setattr(module, "RULE_VERSION", "treatment-proposals-1")
        preview = proposal_preview(patient, actor=patient.account)
        persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
        event = TreatmentEvent.objects.get(patient=patient)
        tokens = {row["id"]: row["current_source_token"] for row in effective_event(event)["sources"]}
        revise_event(patient, event.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                     checked_original=True, expected_sources=tokens, operation_id=uuid.uuid4())
        scope = selection([origin.document], treatment_event_ids=[str(event.pk)])
        job = create_preview(patient, owner.session.session_key, scope, actor=patient.account)
        shared = create_share(patient, patient.account, scope)
        exchange_share_token(shared.token, own.account, reader.session.session_key)
        assert get_preview(patient, owner.session.session_key, job.pk, actor=patient.account).snapshot["treatment_events"]
        assert reader.get(f"/shared/{shared.share.pk}/").status_code == 200
    with pytest.raises(ExportUnavailable):
        get_preview(patient, owner.session.session_key, job.pk, actor=patient.account)
    assert reader.get(f"/shared/{shared.share.pk}/").status_code == 410
    job.refresh_from_db()
    shared.share.refresh_from_db()
    assert job.snapshot == {} and job.status == "INVALIDATED"
    assert shared.share.snapshot == {} and shared.share.invalidated_at is not None
