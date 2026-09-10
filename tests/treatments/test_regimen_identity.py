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
    ("方案甲;方案乙", "方案甲方案乙"),
    ("方案甲；方案乙", "方案甲方案乙"),
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
@pytest.mark.parametrize("doses", [("方案甲1.5mg", "方案甲15mg"), ("方案甲;方案乙", "方案甲方案乙")])
def test_real_persistence_preserves_two_doses_and_source_facts(django_user_model, second_date, doses):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.models import TreatmentCycle, TreatmentEvent, TreatmentRegimen
    from apps.treatments.readmodels import treatment_material
    from tests.documents.test_detail_viewer import _patient
    from tests.treatments.test_derivation_service import fact

    _, patient = _patient(django_user_model, "regimen-dose-" + uuid.uuid4().hex)
    origins = [fact(patient, text=f'{day}给予“{dose}”化疗。')
               for day, dose in [("2024-01-01", doses[0]), (second_date, doses[1])]]
    preview = proposal_preview(patient, actor=patient.account)
    run = persist_proposals(patient, actor=patient.account,
                            expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    events = list(TreatmentEvent.objects.filter(patient=patient))
    assert len(events) == 2
    assert {event.current_content["regimen_text"] for event in events} == set(doses)
    assert {event.evidence.get().fact_id for event in events} == {origin.pk for origin in origins}
    assert TreatmentRegimen.objects.filter(patient=patient).count() == 2
    assert TreatmentCycle.objects.filter(patient=patient, derivation_run=run).count() == 2
    current = treatment_material(patient, actor=patient.account)
    assert len(current["regimens"]) == 2
    assert all(row["status"] == "PENDING" and row["source_valid"] for row in current["regimens"])
    assert len({row["regimen_id"] for row in current["cycles"]}) == 2


@pytest.mark.parametrize("opening,closing", [("“", "”"), ('"', '"'), ("「", "」")])
@pytest.mark.parametrize("separator", [";", "；"])
def test_quoted_separator_keeps_regimen_but_external_separator_ends_plan_scope(opening, closing, separator):
    name = f"方案甲{separator}方案乙"
    text = (f"2024-01-01计划给予{opening}{name}{closing}化疗；"
            f"2024-01-08已给予{opening}方案丙{closing}化疗。")
    result = propose(source(text))
    events = sorted(result["events"], key=lambda row: row["content"]["date"] or "")
    assert [(e["content"]["regimen_text"], e["content"]["date"], e["content"]["occurrence"])
            for e in events] == [(name, "2024-01-01", "PLANNED"), ("方案丙", "2024-01-08", "OCCURRED")]
    assert len(result["cycles"]) == 1
    assert len(result["regimens"]) == 1
    assert "计划" not in events[1]["sources"][0]["raw_text"]
    assert name in events[0]["sources"][0]["raw_text"]


@pytest.mark.parametrize("malformed", ['“方案甲', '“方案甲"', '"方案甲', '「方案甲”'])
def test_unclosed_or_mismatched_quote_does_not_consume_next_statement(malformed):
    first = f"2024-01-01计划给予{malformed}化疗；"
    second = "2024-01-08给予“方案乙”化疗。"
    result = propose(source(first + second))
    event, = [e for e in result["events"] if e["content"]["date"] == "2024-01-08"]
    assert event["content"]["regimen_text"] == "方案乙"
    assert event["content"]["occurrence"] == "OCCURRED"
    assert event["sources"][0]["raw_text"] == second


@pytest.mark.parametrize("next_date", ["", "2024-01-08"])
@pytest.mark.parametrize("action,state", [("已给予", "OCCURRED"), ("已改为", "OCCURRED"),
                                         ("已行", "OCCURRED"), ("可能给予", "UNKNOWN"), ("是否给予", "UNKNOWN")])
def test_dangling_ascii_quote_cannot_pair_with_next_regimens_open_quote(next_date, action, state):
    first = '2024-01-01计划给予"A化疗;'
    second = f'{next_date}{action}"B"化疗。'
    result = propose(source(first + second))
    assert len(result["events"]) == 2
    event, = [row for row in result["events"] if row["content"]["regimen_text"] == "B"]
    assert event["content"]["occurrence"] == state
    assert event["content"]["date"] == (next_date or None)
    assert event["sources"][0]["raw_text"] == second


def test_later_dangling_quote_does_not_break_an_earlier_complete_regimen():
    text = '2024-01-01给予"A;B"化疗；备注"未完'
    result = propose(source(text))
    event, = result["events"]
    assert event["content"]["regimen_text"] == "A;B"
    assert event["content"]["date"] == "2024-01-01"
    assert event["content"]["occurrence"] == "OCCURRED"
    assert len(result["regimens"]) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("qualifier", ["可能", "是否"])
def test_uncertain_quote_boundary_persists_both_original_statements(django_user_model, qualifier):
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.models import TreatmentCycle, TreatmentEvent, TreatmentRegimen
    from tests.documents.test_detail_viewer import _patient
    from tests.treatments.test_derivation_service import fact

    _, patient = _patient(django_user_model, "uncertain-quote-" + uuid.uuid4().hex)
    first = '2024-01-01计划给予"A化疗;'
    second = f'{qualifier}给予"B"化疗。'
    origin = fact(patient, text=first + second)
    preview = proposal_preview(patient, actor=patient.account)
    persist_proposals(patient, actor=patient.account,
                      expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
    events = list(TreatmentEvent.objects.filter(patient=patient))
    assert len(events) == 2
    earlier, = [event for event in events if event.current_content["date"] == "2024-01-01"]
    later, = [event for event in events if event.current_content["regimen_text"] == "B"]
    assert earlier.current_content["occurrence"] == "PLANNED"
    assert later.current_content["occurrence"] == "UNKNOWN"
    assert later.current_content["date"] is None
    assert later.evidence.get().fact_id == origin.pk
    assert later.evidence.get().raw_text == second
    assert not TreatmentRegimen.objects.filter(patient=patient).exists()
    assert not TreatmentCycle.objects.filter(patient=patient).exists()
