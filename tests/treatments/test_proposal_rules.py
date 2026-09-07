"""Synthetic linguistic and organization contracts, independent of private gold."""

from copy import deepcopy

import pytest


def source(text, *, identity="source-a", patient="patient-a", **kwargs):
    return {"id": identity, "patient_id": patient, "text": text, "eligible": True,
            "category": "TREATMENT", "source_kind": "FACT", "text_basis": "ORIGINAL_FACT",
            "source_token": identity, "source_revision": 0, "status": "PENDING",
            "source": {"document_id": identity, "page": 1, "page_id": identity + "-page"}, **kwargs}


def extract(*sources):
    from apps.treatments.proposals import extract_treatment_signals
    return extract_treatment_signals({"sources": list(sources)})


def propose(*sources, labs=()):
    from apps.treatments.proposals import propose_cycles
    return propose_cycles(extract(*sources), labs)


def test_explicit_c3d8_and_actual_date_derive_candidate_anchor_with_provenance():
    text = "治疗经过：2024-03-08 予以“方案甲”C3 D8 化疗。"
    result = propose(source(text))
    assert len(result["cycles"]) == 1
    item = result["cycles"][0]
    assert item["content"]["ordinal"] == 3 and item["content"]["anchor"] == "2024-03-01"
    assert item["content"]["status"] == "PENDING"
    assert item["content"]["anchor_role"] == "EXPLICIT_DAY_OFFSET"
    assert item["content"]["end"] is None
    assert item["content"]["true_d1_claimed"] is False
    for signal in result["events"]:
        for proof in signal["sources"]:
            assert text[proof["start_offset"]:proof["end_offset"]] == proof["raw_text"]


def test_ordinal_after_multiple_dates_remains_unlinked_instead_of_picking_list_position():
    result = extract(source("2024-01-01、2024-01-22、2024-02-12 予以方案甲化疗。第2周期化疗后出现不适。"))
    dated = [s for s in result["signals"] if s["content"]["date"]]
    assert len(dated) == 3
    assert all(s["content"]["cycle_ordinal"] is None for s in dated)
    assert [label["ordinal"] for label in result["labels"]] == [2]
    assert result["labels"][0]["event_date"] is None


@pytest.mark.parametrize("wording", ["计划于2024-01-01给予方案甲化疗", "2024-01-01未行方案甲化疗", "方案甲可能获益", "推荐方案甲治疗"])
def test_plan_negation_and_recommendation_never_create_occurred_cycle(wording):
    result = propose(source(wording))
    assert not result["cycles"]
    assert not any(e["content"]["occurrence"] == "OCCURRED" for e in result["events"])


def test_order_start_check_and_stop_are_preserved_as_order_fields():
    text = "开始时间：2024-02-01 08:00\n长期 合成药物\n执行状态：已退药\n停止时间：2024-02-02 09:00"
    result = propose(source(text, medication_order=True))
    assert not result["cycles"]
    assert len(result["events"]) == 1
    event = result["events"][0]["content"]
    assert event["kind"] == "MEDICATION_ORDER" and event["occurrence"] == "ORDER"
    assert event["date"] is None and event["order_start"]["value"] == "2024-02-01"
    assert event["order_stop"]["value"] == "2024-02-02" and event["execution_status"] == "已退药"


def test_partial_dates_do_not_acquire_d1_or_default_month_day():
    result = propose(source("2024年3月予以方案甲第3周期D8化疗。"))
    assert result["events"][0]["content"]["date_precision"] == "MONTH"
    assert result["cycles"][0]["content"]["anchor"] is None
    assert result["cycles"][0]["content"]["anchor_precision"] == "UNKNOWN"


def test_invalid_or_ranged_cycle_labels_keep_ambiguity_without_legal_ordinal():
    result = extract(source("2024-01-01予以方案甲C0D0化疗。第2至3周期计划治疗。"))
    assert all(s["content"]["cycle_ordinal"] is None for s in result["signals"])
    assert any(label["reason"] == "invalid_or_ranged_label" for label in result["labels"])


def test_chinese_ordinal_and_unicode_offsets_preserve_original_text():
    text = "✅ 2024年2月29日给予方案甲第十二周期第八天化疗。"
    result = propose(source(text))
    assert result["cycles"][0]["content"]["ordinal"] == 12
    assert result["cycles"][0]["content"]["anchor"] == "2024-02-22"
    proof = result["events"][0]["sources"][0]
    assert text[proof["start_offset"]:proof["end_offset"]] == proof["raw_text"]


def test_single_and_plural_date_concurrency_have_different_source_support():
    single = extract(source("2024-01-01予以方案甲化疗，同时给予NK细胞治疗。"))
    plural = extract(source("2024-01-01、2024-01-22予以方案甲化疗，同时给予NK细胞治疗。"))
    cells = [s["content"] for s in single["signals"] if s["content"]["kind"] == "CELL_THERAPY"]
    assert len(cells) == 1 and cells[0]["date"] == "2024-01-01"
    cells = [s["content"] for s in plural["signals"] if s["content"]["kind"] == "CELL_THERAPY"]
    assert len(cells) == 1 and cells[0]["date"] is None
    assert "concurrent_plural_date_unknown" in cells[0]["limitations"]


def test_repeated_history_combines_proofs_but_not_patients_or_unknown_dates():
    text = "2024-01-01给予方案甲C1D1化疗。"
    result = propose(source(text), source(text, identity="source-b"), source(text, identity="source-c", patient="patient-b"))
    assert len(result["cycles"]) == 2
    assert sorted(len(e["sources"]) for e in result["events"]) == [1, 2]
    unknown = propose(source("曾给予方案甲化疗。"), source("曾给予方案甲化疗。", identity="source-b"))
    assert len(unknown["events"]) == 2


def test_same_wording_after_different_regimen_is_a_new_episode():
    result = propose(source("2024-01-01给予方案甲化疗。2024-02-01改为方案乙化疗。2024-03-01再次给予方案甲化疗。"))
    assert len(result["regimens"]) == 3
    assert result["regimens"][0]["normalized_key"] == result["regimens"][2]["normalized_key"]
    assert result["regimens"][0]["id"] != result["regimens"][2]["id"]


def test_admission_only_anchor_is_never_called_executed_chemotherapy():
    result = propose(source("入院日期：2024-01-01；出院日期：2024-01-05。", source_kind="ADMISSION_EVIDENCE"))
    assert len(result["cycles"]) == 1
    item = result["cycles"][0]["content"]
    assert item["anchor_role"] == "ADMISSION_CLUE" and item["ordinal"] is None
    assert item["end"] is None
    assert item["hospital_interval"] == {"start": "2024-01-01", "end": "2024-01-05"}


def test_at_least_three_independent_anchors_required_for_cadence():
    two = propose(source("2024-01-01、2024-01-22给予方案甲化疗。"))
    three = propose(source("2024-01-01、2024-01-22、2024-02-12给予方案甲化疗。"))
    assert not two["regimens"][0]["cadence"]
    assert three["regimens"][0]["cadence"]["median_days"] == 21
    assert three["regimens"][0]["cadence"]["medical_cycle_length"] is False


def test_lab_minima_alone_never_create_treatments_and_no_input_is_mutated():
    from apps.treatments.proposals import extract_treatment_signals, propose_cycles
    material = {"sources": [source("NK细胞计数 230，参考范围100至500。", category="LAB")]}
    before = deepcopy(material)
    signals = extract_treatment_signals(material)
    result = propose_cycles(signals, [{"day": "2024-01-01", "value": "1"}, {"day": "2024-01-22", "value": "1"}])
    assert not result["cycles"] and not result["events"]
    assert material == before


def test_excluded_or_stale_fact_is_not_reintroduced_by_proposal_reader():
    result = propose(source("2024-01-01给予方案甲化疗。", eligible=False, status="EXCLUDED"))
    assert result["events"] == [] and result["cycles"] == []
    assert result["excluded"][0]["reason"] == "source_not_eligible"


def test_explicit_day_without_ordinal_derives_anchor_but_keeps_cycle_number_unknown():
    result = propose(source("2024-03-08给予方案甲D8化疗。"))
    assert result["cycles"][0]["content"]["anchor"] == "2024-03-01"
    assert result["cycles"][0]["content"]["ordinal"] is None


def test_concurrent_cell_event_does_not_create_a_systemic_regimen_switch():
    result = propose(source("2024-01-01予以方案甲化疗，同时给予NK细胞治疗。2024-01-22予以方案甲化疗。"))
    systemic = [r for r in result["regimens"] if r["content"]["text"] == "方案甲"]
    assert len(systemic) == 1 and len(systemic[0]["event_ids"]) == 2


def test_two_dates_with_explicit_day_offsets_join_one_numbered_organization():
    result = propose(source("2024-03-01给予方案甲C3D1化疗。2024-03-08给予方案甲C3D8化疗。"))
    assert len(result["cycles"]) == 1
    assert len(result["cycles"][0]["event_ids"]) == 2
