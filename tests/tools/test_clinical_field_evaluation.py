from copy import deepcopy

import pytest


def _field(key, value, quote="左肺见结节，约12mm。", entity="lesion:01", **kwargs):
    return {"field_key": key, "entity_key": entity, "value": value, "sources": [{"page": 1, "raw_quote": quote}], "status": "PRESENT", **kwargs}


def _gold():
    return {"reports": [{"report_id": "S001-R01", "source_number": 1, "routing_kind": "IMAGING", "page_ranges": [[1, 1]],
                         "fields": [_field("lesion.site", {"text": "左肺"}),
                                    _field("lesion.dimensions", {"components": [{"value": "12", "unit": "mm", "axis": None}], "approximate": True, "raw": "约12mm", "measurement_role": "CURRENT"}),
                                    _field("lesion.laterality", {"code": "LEFT", "raw": "左肺"})]}]}


def _predictions():
    return [{"source_number": 1, "status": "EXTRACTED", "reports": [{"pages": [1], "routing_kind": "IMAGING", "ordinal": 0,
              "fields": [{**field, "source_valid": True, "fragments": [{"page": 1, "raw_text": field["sources"][0]["raw_quote"]}]} for field in _gold()["reports"][0]["fields"]]}]}]


def test_strict_values_sources_units_axis_and_full_failed_denominator():
    from tools.clinical_field_evaluation import evaluate_fields
    gold, actual = _gold(), _predictions()
    result, _ = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == result["fields"]["recall_denominator"] == 3
    actual[0]["reports"][0]["fields"][1]["value"]["components"][0]["unit"] = "cm"
    actual[0]["reports"][0]["fields"][2]["source_valid"] = False
    result, _ = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == 1 and result["fields"]["mismatched"] == 2
    result, _ = evaluate_fields(gold, [{"source_number": 1, "status": "FAILED", "reports": []}])
    assert result["fields"]["missing"] == 3 and result["files"]["failed"] == 1


def test_duplicate_extra_and_absent_target_are_not_removed_from_precision():
    from tools.clinical_field_evaluation import evaluate_fields
    gold, actual = _gold(), _predictions()
    gold["reports"][0]["fields"][2].update(status="ABSENT_NOT_STATED", value=None, sources=[])
    actual[0]["reports"][0]["fields"].append(deepcopy(actual[0]["reports"][0]["fields"][0]))
    result, _ = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == 2 and result["fields"]["extra"] == 2
    assert result["absent_targets"]["contradicted"] == 1
    assert result["fields"]["recall_denominator"] == 2


def test_entity_association_uses_source_clause_and_cannot_mix_values_between_lesions():
    from tools.clinical_field_evaluation import evaluate_fields
    gold, actual = _gold(), _predictions()
    second = deepcopy(gold["reports"][0]["fields"])
    for field in second:
        field["entity_key"] = "lesion:02"
        field["sources"][0]["raw_quote"] = "右肺见结节，约8mm。"
    second[0]["value"]["text"] = "右肺"
    second[1]["value"]["components"][0]["value"] = "8"
    second[2]["value"]["code"] = "RIGHT"
    gold["reports"][0]["fields"].extend(second)
    second_actual = [{**field, "source_valid": True, "fragments": [{"page": 1, "raw_text": "右肺见结节，约8mm。"}]} for field in deepcopy(second)]
    actual[0]["reports"][0]["fields"].extend(second_actual)
    first_value = actual[0]["reports"][0]["fields"][1]["value"]
    second_value = actual[0]["reports"][0]["fields"][4]["value"]
    actual[0]["reports"][0]["fields"][1]["value"], actual[0]["reports"][0]["fields"][4]["value"] = second_value, first_value
    result, audit = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == 4 and result["fields"]["mismatched"] == 2
    assert len(audit[0]["entity_pairs"]) == 3  # report root and two source clauses


def test_prediction_source_set_cannot_omit_failures():
    from tools.clinical_field_evaluation import evaluate_fields
    with pytest.raises(ValueError):
        evaluate_fields(_gold(), [])
