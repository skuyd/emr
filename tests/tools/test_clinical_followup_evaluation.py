from copy import deepcopy

import pytest

from tools.clinical_followup_evaluation import FIELD_KEYS, evaluate_followup, field_source_proof, project_fields


def data():
    quote = "左肺结节为本报告最大病灶，SUVmax3.5。"
    values = [
        ("lesion.suvmax", "lesion:one", dict(values=["3.5"], comparator="EQ", unit=None, approximate=False,
            measurement_role="CURRENT", raw="SUVmax3.5"), quote, "左肺"),
        ("lesion.maximum_scope", "lesion:one", {"code": "REPORT_MAXIMUM", "raw": "本报告最大病灶"}, quote, "左肺"),
        ("comparison.statement", "comparison:one", {"text": "对比前片（2025年）"}, "对比前片（2025年）", ""),
        ("comparison.reference_date", "comparison:one", {"value": "2025", "precision": "YEAR"}, "对比前片（2025年）", ""),
    ]
    gold_fields, predicted = [], []
    for key, entity, value, text, anchor in values:
        gold_fields.append(dict(field_key=key, entity_key=entity, status="PRESENT", value=value, entity_anchor=anchor,
            sources=[dict(page=1, raw_quote=text, ocr_offsets=None, exact_polygon=None)]))
        predicted.append(dict(field_key=key, entity_key=entity.replace("one", "001"), value=deepcopy(value),
            source_valid=True, raw_text=text, fragments=[dict(page=1, raw_text=text)]))
    gold = dict(policy=dict(field_keys=list(FIELD_KEYS), source_numbers=[1]), reports=[dict(
        report_id="S001-R01", source_number=1, routing_kind="IMAGING", page_ranges=[[1, 1]], fields=gold_fields, absence=[])])
    predictions = [dict(source_number=1, status="EXTRACTED", unparsed_page_count=0, reports=[dict(
        pages=[1], routing_kind="IMAGING", fields=predicted)])]
    return gold, predictions


def test_new_task_denominator_and_projection_leave_original_fields_and_failed_sources_intact():
    gold, predictions = data()
    original = dict(field_key="lesion.site", entity_key="lesion:001", value={"text": "左肺"})
    predictions[0]["reports"][0]["fields"].append(original)
    before = deepcopy(predictions)
    metrics, audit = evaluate_followup(gold, predictions)
    assert metrics["fields"]["correct"] == metrics["fields"]["recall_denominator"] == 4
    assert metrics["fields"]["extra"] == 0
    assert predictions == before
    projected = project_fields(predictions, ["lesion.site"])
    assert projected[0]["reports"][0]["fields"] == [original]
    assert len(audit[0]["entity_pairs"]) == 2
    failed = [dict(source_number=1, status="FAILED", reports=[], unparsed_page_count=1)]
    metrics, _ = evaluate_followup(gold, failed)
    assert metrics["fields"]["missing"] == 4 and metrics["files"]["failed"] == 1
    assert project_fields(failed, []) == failed
    with pytest.raises(ValueError):
        evaluate_followup(gold, [])


@pytest.mark.parametrize("source", ["左肺", "右肾见囊肿，SUVmax3.5。", "SUVmax3.5", "左肺结节为本报告最大病灶，SUVmax3.5。跨物理页正文"])
def test_same_page_and_matching_value_do_not_replace_own_suv_source_proof(source):
    gold, predictions = data()
    suv = predictions[0]["reports"][0]["fields"][0]
    suv["fragments"][0]["raw_text"] = source
    metrics, audit = evaluate_followup(gold, predictions)
    assert metrics["fields"]["correct"] == 3
    assert metrics["fields"]["mismatched"] == 1
    assert audit[0]["fields"][0]["source_status"] == "UNVERIFIED"


def test_comparison_date_from_exam_label_on_same_page_is_not_credited():
    gold, predictions = data()
    actual = predictions[0]["reports"][0]["fields"][3]
    actual["fragments"][0]["raw_text"] = "检查日期：2025年"
    metrics, audit = evaluate_followup(gold, predictions)
    assert metrics["fields"]["correct"] == 3 and metrics["fields"]["mismatched"] == 1
    assert audit[0]["fields"][3]["source_status"] == "UNVERIFIED"


def test_qualifier_and_explicit_unit_mismatches_are_not_hidden_by_numeric_equality():
    gold, predictions = data()
    value = predictions[0]["reports"][0]["fields"][0]["value"]
    value.update(comparator="LE", unit="invented", approximate=True)
    metrics, _ = evaluate_followup(gold, predictions)
    assert metrics["fields"]["correct"] == 3 and metrics["fields"]["value_mismatched"] == 1


def test_source_locator_is_checked_per_field_and_unknown_locator_stays_unverified():
    gold, predictions = data()
    expected, actual = gold["reports"][0]["fields"][0], predictions[0]["reports"][0]["fields"][0]
    expected["sources"][0]["ocr_offsets"] = dict(reading_order=9, start_offset=0, end_offset=12)
    assert field_source_proof(expected, actual, True)[0] == "INVALID"
    expected["sources"][0]["ocr_offsets"] = {"guessed": True}
    assert field_source_proof(expected, actual, True)[0] == "UNVERIFIED"


def test_absent_report_fields_and_unmatched_extras_remain_visible():
    gold, predictions = data()
    gold["reports"][0]["fields"] = gold["reports"][0]["fields"][:2]
    gold["reports"][0]["absence"] = [dict(field_key="comparison.statement", status="ABSENT_NOT_STATED_IN_REPORT")]
    metrics, _ = evaluate_followup(gold, predictions)
    assert metrics["fields"]["correct"] == 2 and metrics["fields"]["extra"] == 2
    assert metrics["absent_report_field_checks"] == {"contradicted": 1}
