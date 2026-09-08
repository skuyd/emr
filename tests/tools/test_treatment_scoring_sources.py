"""Only synthetic source text; never import a patient corpus or its predictions."""
from copy import deepcopy

from tests.tools.test_treatment_cycle_evaluation import frozen, prediction
from tools.treatment_cycle_evaluation import score_predictions


def test_same_page_other_statement_cannot_supply_the_source_proof():
    actual = prediction()
    actual["groups"][0]["events"][0]["sources"][0].update(
        raw_text="2024-02-02给予方案乙化疗。", start_offset=200,
        end_offset=220, source_id="different-fact", region_numbers=[8])
    result, _ = score_predictions(frozen(), actual)
    assert result["reported_event_dates"]["TP"] == 0
    assert result["regimen_texts"]["TP"] == 0


def test_duplicate_same_event_cycle_is_an_error_even_without_joint_gold():
    actual = prediction()
    duplicate = deepcopy(actual["groups"][0]["cycles"][0])
    duplicate["id"] = "extra-copy"
    actual["groups"][0]["cycles"].append(duplicate)
    result, _ = score_predictions(frozen(), actual)
    assert result["joint"]["FP"] == 1
    assert result["joint"]["unjudged_predictions"] == 1


def test_unjudged_neighbor_is_not_made_negative_by_a_known_same_page_event():
    gold, actual = frozen(), prediction()
    text = "另一项治疗的方案名称无法辨认。"
    gold["sources"][0]["pages"][0]["events"].append({"mention_id": "unread-neighbor", "source": {
        "source_number": 1, "page": 1, "region_numbers": [8], "text": text}})
    event = deepcopy(gold["events"][0])
    event.update(id="unjudged-event", regimen=None, regimen_variants=[], regimen_conflict=True,
                 mention_ids=["unread-neighbor"])
    gold["events"].append(event)
    case = deepcopy(gold["partial_anchor_cases"][0])
    case.update(event_id="unjudged-event", regimen_identity_judgable=False)
    gold["partial_anchor_cases"].append(case)
    candidate = actual["groups"][0]["events"][0]
    candidate["content"].update(regimen_text="不确定方案", status="PENDING", limitations=["regimen_identity_uncertain"])
    candidate["sources"][0].update(source_id="other-fact", region_numbers=[8], raw_text=text)
    result, _ = score_predictions(gold, actual)
    assert result["regimen_texts"]["FP"] == 0
    assert result["regimen_texts"]["unjudged_predictions"] == 1


def test_a_whole_paragraph_does_not_prove_a_borrowed_date_regimen_pair():
    gold, actual = frozen(), prediction(day="2024-02-02")
    text = "2024-01-01给予方案甲化疗。2024-02-02给予方案乙化疗。"
    gold["sources"][0]["pages"][0]["events"][0]["source"]["text"] = text
    actual["groups"][0]["events"][0]["sources"][0]["raw_text"] = text
    result, _ = score_predictions(gold, actual)
    assert result["regimen_texts"]["TP"] == 0


def test_a_wrong_date_does_not_erase_a_proved_single_assertion_regimen_identity():
    result, _ = score_predictions(frozen(), prediction(day="2024-01-02"))
    assert result["reported_event_dates"]["FP"] == 1
    assert result["regimen_texts"]["TP"] == 1


def test_real_mapping_ignores_supplied_region_numbers_and_retains_ambiguous_quotes_as_unknown():
    from tools.treatment_source_mapping import SourceMapper
    text = "2024-01-01给予方案甲化疗。"
    index = SourceMapper({(1, 1): [{"text": text, "reading_order": i} for i in range(2)]})
    actual = prediction()
    actual["groups"][0]["events"][0]["sources"][0]["region_numbers"] = [1]
    result, trace = score_predictions(frozen(), actual, source_mapper=index)
    assert result["reported_event_dates"]["TP"] == result["reported_event_dates"]["FP"] == 0
    assert result["reported_event_dates"]["unjudged_predictions"] == result["reported_event_dates"]["FN"] == 1
    prediction_receipt = next(row["receipt"] for row in trace["source_mapping"] if row["owner"] == "prediction_event")
    assert prediction_receipt["match_count"] == 2 and prediction_receipt["reason"] == "ambiguous_quote"


def test_page_only_label_locator_does_not_award_a_token_from_another_statement():
    gold, actual = frozen(), prediction()
    gold["unlinked_cycle_labels"] = [{"id": "label", "patient_group": "synthetic-patient", "cycle_ordinal": 2,
        "original_ordinal_judgable": True, "ordinal_to_event_date_judgable": False,
        "source_locations": [{"source_number": 1, "page": 1}]}]
    gold["sources"][0]["pages"][0]["explicit_cycle_labels"] = [{"ordinal": 2, "source": {
        "source_number": 1, "page": 1, "region_numbers": [2], "text": "第2周期方案甲化疗"}}]
    actual["groups"][0]["labels"] = [{"ordinal": 2, "event_date": None, "source_number": 1, "page": 1,
        "raw": "第3周期", "source_context": "第3周期方案乙化疗", "reason": "", "region_numbers": [8]}]
    result, _ = score_predictions(gold, actual)
    assert result["original_ordinals"]["TP"] == 0
