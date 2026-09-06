import copy

import pytest

from tools.phase_three_evaluation import evaluate_predictions, validate_inputs


def fact(text="未见明确转移。", **values):
    return {"page": 1, "category": "DIAGNOSIS", "heading": "诊断", "text": text, **values}


def annotation(number, facts, **values):
    return {"source_number": number, "reviewed_pages": [1], "annotation_complete": True, "facts": facts, **values}


def prediction(number, facts, status="EXTRACTED"):
    return {"source_number": number, "status": status, "facts": facts}


def test_failed_sources_missing_rows_and_extra_candidates_stay_in_denominators():
    gold = [annotation(1, [fact()]), annotation(2, [fact()]), annotation(3, [fact()])]
    actual = [prediction(1, [fact("诊断：未见明确转移。")]), prediction(2, [], "FAILED"),
              prediction(3, [fact("未见明确转移，建议复查。"), fact("另一个无关结论")])]
    result = evaluate_predictions(gold, actual)
    assert result["files"] == {"total": 3, "with_candidates": 2, "no_candidates": 0, "failed": 1}
    assert result["fields"]["correct"] == 1
    assert result["fields"]["mismatched"] == 1
    assert result["fields"]["missing"] == 1
    assert result["fields"]["extra"] == 1
    assert result["fields"]["precision"] == pytest.approx(1/3)
    assert result["fields"]["recall"] == pytest.approx(1/3)


def test_negation_page_category_and_duplicate_count_cannot_be_normalized_away():
    gold = [annotation(1, [fact(), fact(), fact("考虑异常。"), fact("治疗甲", category="TREATMENT")])]
    actual = [prediction(1, [fact("未见明确转移。", page=2), fact("考虑异常。", category="STAGE"),
                            fact("治疗甲", category="TREATMENT", source_valid=False)])]
    result = evaluate_predictions(gold, actual)["fields"]
    assert result["correct"] == 0
    assert result["mismatched"] == 3
    assert result["missing"] == 1
    changed = evaluate_predictions([annotation(1, [fact()])], [prediction(1, [fact("明确转移。")])])
    assert changed["fields"]["correct"] == 0


def test_unjudged_pages_and_clipped_fields_are_explicit_not_zero_positive_sources():
    gold = [annotation(1, [], unjudged_pages=[2], unjudgeable_fields=[{"page": 1, "reason": "clipped"}])]
    result = evaluate_predictions(gold, [prediction(1, [fact(page=2)])])
    assert result["fields"]["unjudgeable_predictions"] == 1
    assert result["fields"]["unjudgeable_source_fields"] == 1
    assert result["fields"]["precision"] is None and result["fields"]["recall"] is None
    assert result["unjudged_pages"] == 1
    assert result["review_burden"]["candidates_to_check"] == 1


def test_record_dates_are_scored_separately_including_unknown_and_source_mismatch():
    gold = [annotation(1, [fact(record_date={"value": "2026-08", "precision": "MONTH"})])]
    actual = [prediction(1, [fact(record_date={"value": "2026-08-01", "precision": "DAY"})])]
    result = evaluate_predictions(gold, actual)
    assert result["fields"]["correct"] == 1
    assert result["record_dates"] == {"correct": 0, "mismatched": 1, "missing_candidate": 0, "unannotated": 0}


def test_source_set_duplicates_and_non_frozen_annotations_are_rejected():
    inventory = {"files": [{"source_number": 1, "ocr_pages": 1}]}
    gold = {"status": "frozen", "sources": [annotation(1, [fact()])]}
    validate_inputs(inventory, gold)
    for broken in [dict(gold, status="in_progress"), dict(gold, sources=gold["sources"]*2),
                   dict(gold, sources=[])]:
        with pytest.raises(ValueError):
            validate_inputs(inventory, broken)
    out_of_bounds = copy.deepcopy(gold)
    out_of_bounds["sources"][0]["facts"][0]["page"] = 2
    with pytest.raises(ValueError):
        validate_inputs(inventory, out_of_bounds)
