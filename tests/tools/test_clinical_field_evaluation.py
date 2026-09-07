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


@pytest.mark.parametrize("replacement", ["右肾见囊肿，约12mm。", "约12mm。", "", "左肺"])
def test_each_field_requires_its_own_frozen_source_clause_not_another_field_anchor(replacement):
    from tools.clinical_field_evaluation import evaluate_fields

    gold, actual = _gold(), _predictions()
    actual[0]["reports"][0]["fields"][1]["fragments"][0]["raw_text"] = replacement
    result, audit = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == 2 and result["fields"]["mismatched"] == 1
    assert result["fields"]["source_unverified"] == 1
    assert result["fields"]["precision_denominator"] == result["fields"]["recall_denominator"] == 3
    assert audit[0]["fields"][1]["source_status"] == "UNVERIFIED"


def test_explicit_frozen_source_location_must_match_even_when_quoted_words_repeat():
    from tools.clinical_field_evaluation import evaluate_fields

    gold, actual = _gold(), _predictions()
    gold["reports"][0]["fields"][1]["sources"][0]["ocr_offsets"] = {
        "reading_order": 2, "start_offset": 0, "end_offset": 13}
    actual[0]["reports"][0]["fields"][1]["fragments"][0].update(reading_order=8, start_offset=0, end_offset=13)
    result, audit = evaluate_fields(gold, actual)
    assert result["fields"]["correct"] == 2 and result["fields"]["mismatched"] == 1
    assert audit[0]["fields"][1]["source_status"] == "INVALID"


def test_rescore_preserves_original_prediction_identity_without_application_replay(tmp_path, monkeypatch):
    import json
    from tools import clinical_field_evaluation as evaluator

    write = lambda name, value: (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic original")
    ocr = tmp_path / "synthetic-ocr.json"
    ocr.write_bytes(b"{}")
    entry = {"source_number": 1, "source_sha256": evaluator.file_hash(source), "ocr_sha256": evaluator.file_hash(ocr),
             "source_path": str(source), "ocr_path": str(ocr)}
    manifest = {"sources": [entry]}
    gold = _gold()
    gold.update(policy={"status": "FROZEN_PRE_PREDICTION", "prediction_read_before_freeze": False,
                        "source_numbers": [1], "declared_task": "synthetic seven-field task"}, sources=[entry])
    actual = _predictions()
    actual[0]["unparsed_page_count"] = 0
    actual[0]["reports"][0]["fields"][1]["fragments"][0]["raw_text"] = "右肾见囊肿，约12mm。"
    for name, value in (("gold.json", gold), ("manifest.json", manifest), ("predictions.json", actual)):
        write(name, value)
    identity = {"gold_sha256": evaluator.file_hash(tmp_path / "gold.json"),
                "manifest_sha256": evaluator.file_hash(tmp_path / "manifest.json"),
                "prediction_content_sha256": evaluator.file_hash(tmp_path / "predictions.json"),
                "parser_files": {"retained-generator.py": "a" * 64}, "dictionary_version": "synthetic", "dictionary_hash": "b" * 64}
    write("generation.json", {"identity": identity})
    def forbidden(*args, **kwargs):
        raise AssertionError("Rescoring must not create fresh predictions")
    monkeypatch.setattr(evaluator, "predict_fields", forbidden)
    arguments = ["--manifest", str(tmp_path / "manifest.json"), "--gold", str(tmp_path / "gold.json"),
                 "--gold-sha256", identity["gold_sha256"], "--predictions", str(tmp_path / "predictions.json"),
                 "--prediction-report", str(tmp_path / "generation.json"), "--report", str(tmp_path / "rescore.json"),
                 "--private-output", str(tmp_path / "rescore-private")]
    assert evaluator.main(arguments) == 0
    report = json.loads((tmp_path / "rescore.json").read_text(encoding="utf-8"))
    assert report["current"]["fields"]["correct"] == 2 and report["current"]["fields"]["source_unverified"] == 1
    assert report["execution_kind"] == "retained_prediction_rescore"
    assert report["identity"]["parser_files"] == identity["parser_files"]
    assert report["identity"]["prediction_content_sha256"] == identity["prediction_content_sha256"]
    assert (tmp_path / "rescore-private/predictions.json").read_bytes() == (tmp_path / "predictions.json").read_bytes()
