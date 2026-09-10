"""Authored synthetic evidence tests; no real gold or patient input is loaded."""
from copy import deepcopy

import pytest

from tools.pathology_molecular_evaluation import EvaluationInputError, evaluate


SOURCE, OCR = "1" * 64, "2" * 64


def panel():
    texts = ["标本编号：SYN-A", "检测项目：SYN-ASSAY", "标记物：PD-L1", "检测结果：TPS 17%"]
    regions = [{"text": text, "reading_order": i,
                "polygon": [[0.1, 0.1+i*0.1], [0.8, 0.1+i*0.1], [0.8, 0.15+i*0.1], [0.1, 0.15+i*0.1]]}
               for i, text in enumerate(texts)]
    original = {"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1, "regions": regions}

    def proof(index, *, label=False):
        block = regions[index]
        colon = block["text"].index("：") + 1
        start, end = (0, colon) if label else (colon, len(block["text"]))
        return {"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1,
                "region_index": index, "reading_order": index, "start_offset": start, "end_offset": end,
                "raw_text": block["text"][start:end], "polygon": deepcopy(block["polygon"])}

    values = [{"label": "SYN-A", "raw": "SYN-A"}, {"label": "SYN-ASSAY", "raw": "SYN-ASSAY"},
              {"code": "PD_L1", "label": "PD-L1", "raw": "PD-L1"},
              {"score_kind": "TPS", "values": ["17"], "comparator": "EQ", "unit": "%", "unit_state": "PRINTED",
               "scale_kind": "PROPORTION", "approximate": False, "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": "TPS 17%"}]
    keys = ["specimen.identity", "assay.identity", "ihc.marker", "ihc.score"]
    links = [{}, {"SPECIMEN": 0}, {"SPECIMEN": 0, "ASSAY": 1}, {"SPECIMEN": 0, "ASSAY": 1, "MARKER": 2}]
    fields, items = [], []
    for i, key in enumerate(keys):
        field = {"gold_id": f"SYN-G{i}", "field_key": key, "expected_value": deepcopy(values[i]),
                 "source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1, "source_role": "CURRENT_RESULT",
                 "value_evidence": [proof(i)], "label_evidence": [proof(i, label=True)],
                 "bindings": {role: f"SYN-G{target}" for role, target in links[i].items()}}
        fields.append(field)
        items.append({"candidate_id": f"run-one-{i}", "field_key": key, "value": deepcopy(values[i]), "source_role": "CURRENT_RESULT",
                      "value_evidence": [proof(i)], "label_evidence": [proof(i, label=True)],
                      "bindings": {role: {"state": "BOUND", "target": deepcopy(items[target]), "proof_evidence": [proof(target)]}
                                   for role, target in links[i].items()}, "mapping_diagnostics": []})
    gold = {"fields": fields, "coverage": [{"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1,
                                           "state": "SCOPED_FIELDS_AND_EXCLUSIONS"}], "negative_boundaries": []}
    predictions = {"pages": [{"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1, "status": "COMPLETE", "items": items}]}
    predicates = {"rules": [], "role_policy": {"patient_admission": ["CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"],
                  "attributed_exclusion": ["QC", "CONTROL", "EXPLANATION", "HISTORICAL_QUOTE", "SUBMITTED_HISTORY"]},
                  "unknown_text_literals": ["", "/", "-", "未知", "未提供", "未注明", "不详"]}
    return gold, predictions, {"pages": [original]}, predicates


def score(case):
    return evaluate(*case)


def last(result):
    return result["assignments"][-1]


def test_complete_context_is_scored_separately_from_one_actual_score():
    result = score(panel())
    assert result["summary"]["field_outcomes"] == {"CORRECT": 4, "MISMATCH": 0, "SOURCE_UNVERIFIED": 0, "MISSING": 0}
    assert result["summary"]["score_fields"]["denominator"] == 1
    assert result["summary"]["context_fields"]["denominator"] == 3
    assert result["assignments"][0]["components"]["specimen_assay_marker_binding"] == {"applicable": False, "status": "NOT_APPLICABLE"}
    assert result["summary"]["components"]["score_kind_and_scale"]["denominator"] == 1
    assert result["summary"]["components"]["original_date_role_and_precision"]["denominator"] == 0


def test_runtime_uuid_change_and_empty_duplicate_ids_do_not_change_pairing():
    case = panel()
    for item in case[1]["pages"][0]["items"]:
        item["candidate_id"] = ""
    assert last(score(case))["status"] == "CORRECT"
    case[1]["pages"][0]["items"].append(deepcopy(case[1]["pages"][0]["items"][-1]))
    result = score(case)
    assert result["summary"]["field_outcomes"]["CORRECT"] == 4
    assert result["summary"]["duplicate_candidates"] == 1


def test_first_wrong_candidate_is_not_replaced_by_later_correct_value():
    case = panel()
    items = case[1]["pages"][0]["items"]
    items.append(deepcopy(items[-1]))
    items[-2]["value"]["values"] = ["91"]
    result = score(case)
    assert last(result)["status"] == "MISMATCH"
    assert last(result)["candidate_ordinal"] == 3
    assert result["summary"]["duplicate_candidates"] == 1


@pytest.mark.parametrize("key,value", [("values", ["NaN"]), ("values", ["18"]), ("comparator", "LE"),
    ("approximate", True), ("unit", None), ("unit_state", "NOT_PRINTED"), ("score_kind", "CPS"),
    ("scale_kind", "SCORE"), ("assertion", "NEGATIVE")])
def test_independent_score_semantics_cannot_be_replaced_by_matching_numeric_text(key, value):
    case = panel()
    case[1]["pages"][0]["items"][-1]["value"][key] = value
    assert last(score(case))["status"] == "MISMATCH"


def test_null_unit_is_distinct_from_missing_key_and_decimal_equivalence_is_limited():
    case = panel()
    expected = case[0]["fields"][-1]["expected_value"]
    actual = case[1]["pages"][0]["items"][-1]["value"]
    expected.update(unit=None, unit_state="NOT_PRINTED")
    actual.update(unit=None, unit_state="NOT_PRINTED", values=["17.0"])
    expected["raw"] = actual["raw"] = "TPS 17"
    case[2]["pages"][0]["regions"][-1]["text"] = "检测结果：TPS 17"
    for proof in case[0]["fields"][-1]["value_evidence"] + case[1]["pages"][0]["items"][-1]["value_evidence"]:
        proof["end_offset"] -= 1
        proof["raw_text"] = proof["raw_text"][:-1]
    assert last(score(case))["status"] == "CORRECT"
    del actual["unit"]
    assert last(score(case))["components"]["raw_unit_and_unit_state"]["status"] == "MISMATCH"


@pytest.mark.parametrize("key,value", [("raw_text", "TPS17%"), ("reading_order", 87), ("end_offset", 999),
    ("polygon", [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]), ("ocr_sha256", "7" * 64)])
def test_every_locator_and_raw_unicode_must_match_the_independent_original_index(key, value):
    case = panel()
    case[1]["pages"][0]["items"][-1]["value_evidence"][0][key] = value
    assert last(score(case))["status"] == "SOURCE_UNVERIFIED"


def test_a_good_claim_does_not_cancel_an_additional_contradictory_locator():
    case = panel()
    item = case[1]["pages"][0]["items"][-1]
    bad = deepcopy(item["value_evidence"][0])
    bad["char_start"] = 0
    item["value_evidence"].append(bad)
    assert last(score(case))["status"] == "SOURCE_UNVERIFIED"


@pytest.mark.parametrize("missing", ["label", "association", "target_source"])
def test_matching_value_without_own_label_or_association_evidence_stays_unverified(missing):
    case = panel()
    item = case[1]["pages"][0]["items"][-1]
    if missing == "label":
        item["label_evidence"] = []
    elif missing == "association":
        item["bindings"]["MARKER"]["proof_evidence"] = []
    else:
        item["bindings"]["MARKER"]["target"]["value_evidence"] = []
    assert last(score(case))["status"] == "SOURCE_UNVERIFIED"


def test_same_marker_words_from_another_original_row_are_wrong_context():
    case = panel()
    other = deepcopy(case[2]["pages"][0]["regions"][2])
    other["reading_order"] = 4
    other["polygon"] = [[0.1, 0.8], [0.8, 0.8], [0.8, 0.9], [0.1, 0.9]]
    case[2]["pages"][0]["regions"].append(other)
    binding = case[1]["pages"][0]["items"][-1]["bindings"]["MARKER"]
    for proof in binding["target"]["value_evidence"] + binding["target"]["label_evidence"] + binding["proof_evidence"]:
        proof.update(region_index=4, reading_order=4, polygon=deepcopy(other["polygon"]))
    assert last(score(case))["components"]["specimen_assay_marker_binding"]["status"] == "MISMATCH"


def test_unknown_context_does_not_become_a_numeric_only_success():
    case = panel()
    case[1]["pages"][0]["items"][-1]["bindings"]["MARKER"] = {"state": "UNKNOWN", "target": None, "proof_evidence": [], "reason": "NOT_STATED"}
    assert last(score(case))["status"] == "SOURCE_UNVERIFIED"


@pytest.mark.parametrize("status", ["FAILED", "NOT_RUN", "INVALID"])
def test_failed_or_not_run_pages_keep_fixed_denominators_without_successful_empty_execution(status):
    case = panel()
    case[1]["pages"][0]["status"] = status
    result = score(case)
    assert result["summary"]["field_outcomes"]["MISSING"] == 4
    assert result["summary"]["execution"]["complete_pages"] == 0


def test_missing_page_and_duplicate_envelopes_do_not_create_successful_execution():
    case = panel()
    case[1]["pages"] = []
    assert score(case)["summary"]["execution"]["not_run_pages"] == 1
    case = panel()
    case[1]["pages"].append(deepcopy(case[1]["pages"][0]))
    result = score(case)
    assert result["summary"]["execution"]["invalid_pages"] == 1
    assert result["summary"]["field_outcomes"]["MISSING"] == 4


def test_unlocated_candidate_and_originally_unreviewed_page_are_retained():
    case = panel()
    page2 = deepcopy(case[2]["pages"][0])
    page2["page"] = 2
    case[2]["pages"].append(page2)
    case[0]["coverage"].append({"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 2, "state": "UNREVIEWED_UNJUDGED"})
    case[1]["pages"].append({"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 2, "status": "COMPLETE", "items": []})
    case[1]["pages"][0]["items"][-1].update(value_evidence=[], label_evidence=[])
    result = score(case)
    assert last(result)["status"] == "MISSING"
    assert result["summary"]["unlocated_candidates"] == 1
    assert result["summary"]["scope"]["unreviewed_pages"] == 1


@pytest.mark.parametrize("bad", ["duplicate_id", "missing_reference", "cycle", "original_sha", "original_words"])
def test_malformed_gold_or_inconsistent_independent_originals_are_input_errors(bad):
    case = panel()
    fields = case[0]["fields"]
    if bad == "duplicate_id":
        fields.append(deepcopy(fields[0]))
    elif bad == "missing_reference":
        fields[-1]["bindings"]["SPECIMEN"] = "absent"
    elif bad == "cycle":
        fields[0]["bindings"] = {"MARKER": fields[-1]["gold_id"]}
    elif bad == "original_sha":
        case[2]["pages"][0]["ocr_sha256"] = "9" * 64
    else:
        case[2]["pages"][0]["regions"][0]["text"] = "Different original"
    with pytest.raises(EvaluationInputError):
        score(case)


@pytest.mark.parametrize("role,expected", [("CURRENT_RESULT", "FALSE_ADMISSION"), ("CONTROL", "EXCLUDED_ROLE"), ("UNKNOWN", "UNJUDGED_ROLE")])
def test_only_the_explicit_negative_region_and_its_predicate_is_judged(role, expected):
    case = panel()
    field = case[0]["fields"].pop()
    case[0]["negative_boundaries"] = [{"negative_id": "SYN-N", "source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1,
                                      "evidence": field["value_evidence"] + field["label_evidence"]}]
    case[3]["rules"] = [{"negative_id": "SYN-N", "predicate": {"field_keys": ["ihc.score"], "condition": "ANY"}}]
    case[1]["pages"][0]["items"][-1]["source_role"] = role
    result = score(case)
    assert result["candidate_diagnostics"][-1]["classification"] == expected
    assert result["summary"]["false_admissions"] == int(expected == "FALSE_ADMISSION")
    case[1]["pages"][0]["status"] = "FAILED"
    assert score(case)["summary"]["negative_regions"]["executed"] == 0


def test_invalid_page_envelope_does_not_erase_its_candidates_from_accounting():
    case = panel()
    case[1]["pages"][0]["page"] = 2
    result = score(case)
    assert result["summary"]["candidate_count"] == 4
    assert result["summary"]["candidate_classifications"]["INVALID_PAGE_ENVELOPE"] == 4
    assert result["summary"]["field_outcomes"]["MISSING"] == 4


def test_first_value_proof_on_another_page_is_not_a_valid_item_envelope():
    case = panel()
    case[1]["pages"][0]["items"][-1]["value_evidence"][0]["page"] = 2
    result = score(case)
    assert last(result)["status"] == "MISSING"
    assert result["candidate_diagnostics"][-1]["classification"] == "INVALID_ITEM_ENVELOPE"


def test_overlapping_exclusion_regions_do_not_hide_a_later_applicable_predicate():
    case = panel()
    field = case[0]["fields"].pop()
    boundary = {"source_sha256": SOURCE, "ocr_sha256": OCR, "page": 1, "evidence": field["value_evidence"]}
    case[0]["negative_boundaries"] = [{**deepcopy(boundary), "negative_id": "SYN-N1"}, {**deepcopy(boundary), "negative_id": "SYN-N2"}]
    case[3]["rules"] = [{"negative_id": "SYN-N1", "predicate": {"field_keys": ["specimen.histology"], "condition": "ANY"}},
                         {"negative_id": "SYN-N2", "predicate": {"field_keys": ["ihc.score"], "condition": "ANY"}}]
    result = score(case)
    assert result["summary"]["false_admissions"] == 1
    assert result["candidate_diagnostics"][-1]["negative_id"] == "SYN-N2"


def test_explicit_gold_denominator_is_not_silently_replaced_by_list_length():
    case = panel()
    case[0]["denominators"] = {"target_fields": 5, "score_fields": 1, "identity_and_metadata_fields": 3}
    with pytest.raises(EvaluationInputError):
        score(case)


def test_same_original_date_position_cannot_swap_collection_and_report_roles():
    case = panel()
    expected, actual = case[0]["fields"][-1], case[1]["pages"][0]["items"][-1]
    expected["field_key"], actual["field_key"] = "assay.report_date", "assay.collection_date"
    expected["expected_value"] = {"value": "2030-02-03", "precision": "DAY"}
    actual["value"] = deepcopy(expected["expected_value"])
    del expected["bindings"]["MARKER"]
    del actual["bindings"]["MARKER"]
    block = case[2]["pages"][0]["regions"][-1]
    block["text"] = "报告日期：2030-02-03"
    for node in (expected, actual):
        for role, bounds in (("value_evidence", (5, 15)), ("label_evidence", (0, 5))):
            proof = node[role][0]
            proof.update(start_offset=bounds[0], end_offset=bounds[1], raw_text=block["text"][slice(*bounds)])
    result = score(case)
    assert last(result)["status"] == "MISMATCH"
    assert last(result)["components"]["original_date_role_and_precision"] == {"applicable": True, "status": "MISMATCH"}


def test_unit_ocr_substitution_is_not_a_permitted_normalization():
    case = panel()
    case[0]["fields"][-1]["expected_value"]["unit"] = "mmol/L"
    case[1]["pages"][0]["items"][-1]["value"]["unit"] = "mmo1/L"
    assert last(score(case))["components"]["raw_unit_and_unit_state"]["status"] == "MISMATCH"
