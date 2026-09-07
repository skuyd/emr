import copy
import hashlib
import json

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


def test_joint_f1_counts_mismatches_in_both_precision_and_recall():
    gold = [annotation(1, [fact(), fact("第二条事实"), fact("第三条事实")])]
    actual = [prediction(1, [fact(), fact("第二条事实不完整"), fact("完全无关的候选")])]
    fields = evaluate_predictions(gold, actual)["fields"]
    assert fields["correct"] == 1
    assert fields["precision_denominator"] == 3
    assert fields["recall_denominator"] == 3
    assert fields["f1"] == pytest.approx(1 / 3)


def frozen_inventory(tmp_path):
    from dataclasses import asdict
    from tests.labs.test_phase_two_layout import page

    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic fact evaluation original")
    identity = hashlib.sha256(source.read_bytes()).hexdigest()
    ocr_page = page([(.1, [(.1, "诊断：合成病名。")])])
    encoded = asdict(ocr_page)
    encoded["provider_metadata"] = dict(ocr_page.provider_metadata)
    cache = tmp_path / f"{identity}.json"
    cache.write_text(json.dumps({"source_file_hash": identity, "pages": [encoded]}), encoding="utf-8")
    return {"files": [{
        "source_number": 1, "source_file_hash": identity, "source_path": str(source),
        "ocr_cache_path": str(cache), "ocr_cache_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "ocr_pages": 1,
    }]}


@pytest.mark.django_db(transaction=True)
def test_fact_replay_defaults_to_production_dictionary_and_records_persisted_identity(tmp_path):
    from apps.labs.dictionary import phase_two_dictionary
    from apps.processing.models import ParsingVersion
    from tools.phase_three_evaluation import predict_sources

    predictions, execution = predict_sources(frozen_inventory(tmp_path))
    version = ParsingVersion.objects.get(active=True)
    assert predictions[0]["facts"][0]["text"] == "诊断：合成病名。"
    assert version.dictionary_version == phase_two_dictionary().version
    assert version.dictionary_hash == phase_two_dictionary().content_hash
    assert execution["dictionary_version"] == version.dictionary_version
    assert execution["dictionary_hash"] == version.dictionary_hash


@pytest.mark.django_db(transaction=True)
def test_fact_replay_can_explicitly_reproduce_historical_dictionary(tmp_path):
    from apps.labs.dictionary import default_dictionary
    from apps.processing.models import ParsingVersion
    from tools.phase_three_evaluation import predict_sources

    historical = default_dictionary()
    predictions, execution = predict_sources(frozen_inventory(tmp_path), dictionary=historical)
    version = ParsingVersion.objects.get(active=True)
    assert predictions[0]["status"] == "EXTRACTED"
    assert (version.dictionary_version, version.dictionary_hash) == (historical.version, historical.content_hash)
    assert execution["dictionary_hash"] == historical.content_hash
