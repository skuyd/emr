from copy import deepcopy

import pytest


def frozen():
    return {"cycles": [], "sources": [{"source_number": 1, "ocr_pages": 1, "pages": [
        {"page": 1, "patient_group": "synthetic-patient", "review_state": "ORIGINAL_AND_OCR_REVIEWED", "relevance": "TREATMENT_HISTORY",
         "events": [{"mention_id": "mention", "source": {"source_number": 1, "page": 1,
                    "text": "2024-01-01给予方案甲化疗。", "region_numbers": [1]}}]}]}],
        "events": [{"id": "event", "patient_group": "synthetic-patient", "kind": "SYSTEMIC_TREATMENT", "date": "2024-01-01",
                    "date_precision": "DAY", "regimen": "方案甲", "regimen_variants": ["方案甲"], "mention_ids": ["mention"]}],
        "partial_anchor_cases": [{"event_id": "event", "reported_event_day": "2024-01-01", "event_day_judgable": True,
                                  "literal_date_judgable": True, "regimen_identity_judgable": True}],
        "unlinked_cycle_labels": [], "quality_protocol": {}}


def prediction(*, day="2024-01-01", patient="synthetic-patient", source=1):
    event = {"id": "pred-event", "content": {"date": day, "date_precision": "DAY", "kind": "SYSTEMIC_TREATMENT", "occurrence": "OCCURRED",
             "regimen_text": "方案甲", "cycle_ordinal": None, "cycle_day": None}, "sources": [{"source_number": source, "page": 1,
             "raw_text": "2024-01-01给予方案甲化疗。", "region_numbers": [1]}]}
    cycle = {"id": "pred-cycle", "event_ids": [event["id"]], "content": {"anchor": day, "true_d1_claimed": False, "ordinal": None, "end": None}}
    return {"files": [{"source_number": 1, "status": "success"}],
            "groups": [{"patient_group": patient, "events": [event], "cycles": [cycle], "labels": [], "regimens": []}]}


def test_date_correctness_cannot_establish_joint_cycle_target_without_joint_positive_gold():
    from tools.treatment_cycle_evaluation import score_predictions
    report, _ = score_predictions(frozen(), prediction())
    assert report["reported_event_dates"]["TP"] == 1
    assert report["joint"]["TP"] == 0 and report["joint"]["precision"] is None
    assert report["joint"]["unjudged_predictions"] == 1
    assert report["target_status"] == "NOT_ESTABLISHED_INSUFFICIENT_JOINT_GOLD"


def test_wrong_explicit_date_is_a_judged_source_error_even_when_joint_truth_is_unknown():
    from tools.treatment_cycle_evaluation import score_predictions
    report, _ = score_predictions(frozen(), prediction(day="2024-01-02"))
    assert report["reported_event_dates"]["TP"] == 0
    assert report["reported_event_dates"]["FP"] == report["reported_event_dates"]["FN"] == 1
    assert report["joint"]["FP"] == 1 and report["source_support_assertion_errors"] >= 1


def test_cross_patient_and_duplicate_claims_never_earn_additional_true_positives():
    from tools.treatment_cycle_evaluation import score_predictions
    crossed, _ = score_predictions(frozen(), prediction(patient="other-patient"))
    assert crossed["reported_event_dates"]["TP"] == 0 and crossed["joint"]["FP"] == 1
    duplicated = prediction()
    extra = deepcopy(duplicated["groups"][0]["events"][0])
    extra["id"] = "extra-event"
    duplicated["groups"][0]["events"].append(extra)
    report, _ = score_predictions(frozen(), duplicated)
    assert report["reported_event_dates"]["TP"] == 1 and report["reported_event_dates"]["FP"] == 1


def test_unknown_original_page_is_not_a_blanket_negative():
    from tools.treatment_cycle_evaluation import score_predictions
    gold = frozen()
    gold["partial_anchor_cases"] = []
    page = gold["sources"][0]["pages"][0]
    page.update(review_state="OCR_REVIEWED_ORIGINAL_PENDING", relevance="LAB_ONLY", events=[])
    report, _ = score_predictions(gold, prediction())
    assert report["reported_event_dates"]["FP"] == 0
    assert report["reported_event_dates"]["unjudged_predictions"] == 1


def test_original_ordinal_token_is_scored_separately_from_its_unknown_date_association():
    from tools.treatment_cycle_evaluation import score_predictions
    gold = frozen()
    gold["unlinked_cycle_labels"] = [{"id": "label", "patient_group": "synthetic-patient", "cycle_ordinal": 2,
        "original_ordinal_judgable": True, "ordinal_to_event_date_judgable": False, "source_locations": [{"source_number": 1, "page": 1}]}]
    actual = prediction()
    gold["sources"][0]["pages"][0]["explicit_cycle_labels"] = [{"ordinal": 2, "source": {
        "source_number": 1, "page": 1, "region_numbers": [2], "text": "第2周期方案甲化疗"}}]
    actual["groups"][0]["labels"] = [{"ordinal": 2, "event_date": None, "source_number": 1, "page": 1, "reason": "",
        "raw": "第2周期", "source_context": "第2周期方案甲化疗", "start_offset": 0, "end_offset": 4, "region_numbers": [2]}]
    report, _ = score_predictions(gold, actual)
    assert report["original_ordinals"]["TP"] == 1
    assert report["ordinal_date_links"]["TP"] == 0 and report["ordinal_date_links"]["precision"] is None


def test_every_input_must_remain_in_the_prediction_inventory_including_failures():
    from tools.treatment_cycle_evaluation import score_predictions
    actual = prediction()
    actual["files"] = []
    with pytest.raises(ValueError):
        score_predictions(frozen(), actual)


def test_regimen_identity_is_judged_independently_of_an_unknown_event_date():
    from tools.treatment_cycle_evaluation import score_predictions
    gold = frozen()
    gold["events"][0].update(date=None, date_precision="UNKNOWN")
    gold["partial_anchor_cases"][0].update(reported_event_day=None, event_day_judgable=False, literal_date_judgable=False)
    actual = prediction(day=None)
    actual["groups"][0]["events"][0]["content"]["date_precision"] = "UNKNOWN"
    report, _ = score_predictions(gold, actual)
    assert report["regimen_texts"]["TP"] == 1 and report["regimen_texts"]["FN"] == 0
    assert report["reported_event_dates"]["TP"] == report["reported_event_dates"]["FN"] == 0


@pytest.mark.django_db(transaction=True)
def test_real_frozen_ocr_pipeline_builds_and_persists_unconfirmed_proposals_without_gold(tmp_path):
    import json
    from dataclasses import asdict
    from apps.labs.dictionary import current_dictionary
    from apps.treatments.models import TreatmentCycle, TreatmentDerivationRun
    from tests.labs.test_phase_two_layout import page
    from tools.treatment_cycle_evaluation import file_digest, predict_sources, score_predictions
    from tools.treatment_source_mapping import SourceMapper

    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic treatment original")
    identity = file_digest(source)
    original = page([(.03, [(.05, "治疗经过：")]), (.07, [(.05, "2024-01-01给予方案甲化疗C1D1。")])])
    encoded = asdict(original)
    encoded["provider_metadata"] = dict(original.provider_metadata)
    cache = tmp_path / (identity + ".json")
    cache.write_text(json.dumps({"source_file_hash": identity, "pages": [encoded]}), encoding="utf-8")
    manifest = {"sources": [{"source_number": 1, "source_sha256": identity, "source_path": str(source),
                            "ocr_path": str(cache), "ocr_sha256": file_digest(cache), "ocr_pages": 1}]}
    source_mapper = SourceMapper.from_manifest(manifest)
    predictions, execution = predict_sources(manifest, {1: "synthetic-patient"}, current_dictionary(), source_mapper=source_mapper)
    assert len(predictions["files"]) == 1 and predictions["files"][0]["status"] != "failed"
    group, = predictions["groups"]
    cycle, = group["cycles"]
    assert cycle["content"]["ordinal"] == 1 and cycle["content"]["anchor"] == "2024-01-01"
    assert group["patient_group"] == "synthetic-patient" and all(row["source_number"] == 1 for event in group["events"] for row in event["sources"])
    assert group["persisted_result_counts"]["cycles"] == TreatmentCycle.objects.count() == 1
    assert TreatmentDerivationRun.objects.count() == 1 and TreatmentCycle.objects.get().current_content["status"] == "PENDING"
    assert execution["persistence_and_comparison"] is True
    assert all(proof["mapping"]["status"] == "MATCHED" for event in group["events"] for proof in event["sources"])
    assert all(label["mapping"]["status"] == "MATCHED" for label in group["labels"])
    gold = frozen()
    gold["sources"][0]["pages"][0]["events"][0]["source"].update(
        text="2024-01-01给予方案甲化疗C1D1。", region_numbers=[2])
    report, trace = score_predictions(gold, predictions, source_mapper=source_mapper)
    assert report["reported_event_dates"]["TP"] == report["regimen_texts"]["TP"] == 1
    assert report["joint"]["TP"] == 0
    assert trace["source_mapping"] and all(row["receipt"]["status"] == "MATCHED" for row in trace["source_mapping"])
