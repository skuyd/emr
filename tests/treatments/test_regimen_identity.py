"""Literal regimen differences survive automatic grouping and persistence."""

import uuid

import pytest

from apps.treatments.signals import normalized_regimen
from tests.treatments.test_proposal_rules import propose, source


DISTINCT_REGIMENS = [
    ("方案甲1.5mg", "方案甲15mg"),
    ("方案甲1-5mg", "方案甲15mg"),
    ("方案甲1/5mg", "方案甲15mg"),
    ("方案甲1:5", "方案甲15"),
    ("方案甲、方案乙", "方案甲方案乙"),
    ("药物甲(mg/kg)", "药物甲mgkg"),
]


@pytest.mark.parametrize("left,right", DISTINCT_REGIMENS)
@pytest.mark.parametrize("second_date", ["2024-01-01", "2024-01-08"])
def test_dose_and_combination_punctuation_never_collapses_events_or_regimens(left, right, second_date):
    texts = [f"2024-01-01给予“{left}”化疗。", f"{second_date}给予“{right}”化疗。"]
    result = propose(*(source(text, identity=f"source-{i}") for i, text in enumerate(texts)))
    assert len(result["events"]) == 2
    assert {event["content"]["regimen_text"] for event in result["events"]} == {left, right}
    assert len(result["regimens"]) == 2
    assert len({item["normalized_key"] for item in result["regimens"]}) == 2
    assert len(result["cycles"]) == 2
    assert len({cycle["regimen_id"] for cycle in result["cycles"]}) == 2
    for event in result["events"]:
        assert len(event["sources"]) == 1
        proof = event["sources"][0]
        original = texts[int(proof["source_id"].removeprefix("source-"))]
        assert proof["raw_text"] == original[proof["start_offset"]:proof["end_offset"]]


@pytest.mark.parametrize("left,right", [
    ("方案甲 １．５ ｍｇ", "方案甲1.5mg"),
    ("方案甲１／５ｍｇ", "方案甲1/5mg"),
    ("方案甲Ａ ＋ Ｂ", "方案甲a+b"),
])
def test_representation_only_normalization_still_groups_equivalent_literals(left, right):
    assert normalized_regimen(left) == normalized_regimen(right)
    result = propose(source(f"2024-01-01给予“{left}”化疗。", identity="one"),
                     source(f"2024-01-08给予“{right}”化疗。", identity="two"))
    assert len(result["events"]) == 2
    assert len(result["regimens"]) == 1
    assert len(result["regimens"][0]["event_ids"]) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("second_date", ["2024-01-01", "2024-01-08"])
def test_real_persistence_preserves_two_doses_and_source_facts(django_user_model, second_date):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.models import TreatmentCycle, TreatmentEvent, TreatmentRegimen
    from apps.treatments.readmodels import treatment_material
    from tests.documents.test_detail_viewer import _patient
    from tests.treatments.test_derivation_service import fact

    _, patient = _patient(django_user_model, "regimen-dose-" + uuid.uuid4().hex)
    origins = [fact(patient, text=f'{day}给予“{dose}”化疗。')
               for day, dose in [("2024-01-01", "方案甲1.5mg"), (second_date, "方案甲15mg")]]
    preview = proposal_preview(patient, actor=patient.account)
    run = persist_proposals(patient, actor=patient.account,
                            expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    events = list(TreatmentEvent.objects.filter(patient=patient))
    assert len(events) == 2
    assert {event.current_content["regimen_text"] for event in events} == {"方案甲1.5mg", "方案甲15mg"}
    assert {event.evidence.get().fact_id for event in events} == {origin.pk for origin in origins}
    assert TreatmentRegimen.objects.filter(patient=patient).count() == 2
    assert TreatmentCycle.objects.filter(patient=patient, derivation_run=run).count() == 2
    current = treatment_material(patient, actor=patient.account)
    assert len(current["regimens"]) == 2
    assert all(row["status"] == "PENDING" and row["source_valid"] for row in current["regimens"])
    assert len({row["regimen_id"] for row in current["cycles"]}) == 2
