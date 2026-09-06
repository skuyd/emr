import importlib
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from apps.labs.dictionary import load_dictionary


ROOT = Path(__file__).resolve().parents[2]
DICTIONARIES = ROOT / "apps/labs/dictionaries"
LEGACY_BYTE_HASHES = {
    "lf": "c60dbdab8af49d30bdcbf7092b91fb721c977eb9df6f4c099fcc91c712879df1",
    "crlf": "10b0d914bf6bfe11417148af817acc325abe94fb1d701254a9c0618af07353de",
}


def runner():
    # The first RED must identify the missing gate, rather than a collection error.
    assert importlib.util.find_spec("apps.labs.regression") is not None, "fixed regression gate is missing"
    return importlib.import_module("apps.labs.regression").run_fixed_regression


@pytest.fixture
def dictionary():
    return load_dictionary(DICTIONARIES / "phase-two.json")


def test_fixed_gate_executes_all_target_types_and_separate_context_and_gene_cases(dictionary):
    report = runner()(dictionary)

    assert report["passed"], report["failures"]
    assert report["dataset_kind"] == "SYNTHETIC"
    assert report["scope"] == "phase_two"
    assert report["real_accuracy"] == "not_evaluated"
    assert report["coverage"]["target_total"] == 125
    assert report["coverage"]["target_present"] == 125
    assert report["coverage"]["target_exercised"] == 125
    assert report["coverage"]["missing_target_codes"] == []
    assert report["counts"]["target_positive"]["assessed"] == 125
    assert report["counts"]["target_negative"]["assessed"] == 125
    assert report["counts"]["alias_context"]["assessed"] == 23
    assert report["counts"]["gene_positive"]["assessed"] == 50
    assert report["counts"]["target_parser"]["assessed"] == 373
    assert set(report["result_types"]) == {"numeric", "comparator", "qualitative", "semi_quantitative", "status"}
    assert all(counts["correct"] == counts["expected"] for counts in report["fields"].values())
    assert report["counts"]["severe"]["escaped"] == 0
    assert report["counts"]["severe"]["blocked"] >= 6


def test_frozen_gate_reports_are_deterministic_and_json_serializable(dictionary):
    first = runner()(dictionary)
    second = runner()(dictionary)

    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert len(first["corpus_sha256"]) == len(first["parser_sha256"]) == 64
    assert first["dictionary_sha256"] == dictionary.content_hash
    assert "apps/labs/extraction.py" in first["parser_files"]
    assert "apps/labs/layout.py" in first["parser_files"]


def test_missing_target_cannot_be_replaced_by_gene_count_or_shrinking_corpus(dictionary):
    without = replace(dictionary, indicators=tuple(item for item in dictionary.indicators if item.code != "LAB_VITAMIN_B12"))
    report = runner()(without)

    assert not report["passed"]
    assert report["coverage"]["target_total"] == 125
    assert report["coverage"]["missing_target_codes"] == ["LAB_VITAMIN_B12"]
    assert report["coverage"]["gene_present"] == 50
    assert report["counts"]["target_positive"]["assessed"] == 125
    assert report["counts"]["target_parser"]["assessed"] == 373
    assert any(failure["reason"] == "missing_target" for failure in report["failures"])


def test_removing_alias_causes_fixed_expected_mapping_failure(dictionary):
    items = tuple(
        replace(item, aliases=tuple(alias for alias in item.aliases if alias != "VitB12"))
        if item.code == "LAB_VITAMIN_B12" else item
        for item in dictionary.indicators
    )
    report = runner()(replace(dictionary, indicators=items))

    assert not report["passed"]
    assert any(failure["case_id"] == "positive:LAB_VITAMIN_B12" for failure in report["failures"])
    assert report["fields"]["standard_code"]["correct"] < report["fields"]["standard_code"]["expected"]


def test_context_negative_can_fail_while_all_fixed_positive_aliases_still_match(dictionary):
    items = tuple(
        replace(item, aliases=tuple(alias for alias in item.aliases if alias != "白细胞"))
        if item.code == "LAB_URINE_WBC" else item
        for item in dictionary.indicators
    )
    report = runner()(replace(dictionary, indicators=items))

    assert not report["passed"]
    assert report["counts"]["target_positive"]["passed"] == 125
    assert report["counts"]["target_negative"]["failed"] > 0
    assert any(failure["case_id"] == "negative:LAB_WBC" for failure in report["failures"])


def test_abstaining_parser_cannot_pass_by_reducing_denominator(dictionary, monkeypatch):
    from apps.labs import extraction

    monkeypatch.setattr(extraction, "extract_observations", lambda pages, dictionary: ())
    report = runner()(dictionary)

    assert not report["passed"]
    assert report["fields"]["raw_value"]["expected"] > 373
    assert report["fields"]["raw_value"]["correct"] == 0
    assert report["fields"]["raw_value"]["recall"] == 0
    assert report["counts"]["severe"]["escaped"] > 0


def test_releasing_ambiguous_values_without_quality_reasons_fails_severe_gate(dictionary, monkeypatch):
    from apps.labs import extraction

    extract = extraction.extract_observations

    def unsafe_extraction(pages, dictionary):
        return tuple(replace(item, capability_level="STABLE", quality_issues=[]) for item in extract(pages, dictionary))

    monkeypatch.setattr(extraction, "extract_observations", unsafe_extraction)
    report = runner()(dictionary)

    assert not report["passed"]
    assert report["counts"]["severe"]["escaped"] == report["counts"]["severe"]["total"]
    assert report["fields"]["raw_value"]["missed"] == 0
    assert any(failure.get("field") == "severe_error_blocked" for failure in report["failures"])


@pytest.mark.parametrize("mutation", ["capability_only", "issue_only", "both", "confidence_only"])
def test_normal_controls_cannot_all_be_routed_to_review_with_fields_unchanged(dictionary, monkeypatch, mutation):
    from apps.labs import extraction

    extract = extraction.extract_observations

    def overcautious_extraction(pages, dictionary):
        observations = []
        for item in extract(pages, dictionary):
            changes = {}
            if mutation in {"capability_only", "both"}:
                changes["capability_level"] = "SEARCH_ONLY"
            if mutation in {"issue_only", "both"}:
                changes["quality_issues"] = [*item.quality_issues, {
                    "code": "recognition_uncertain", "fields": ["raw_value"],
                    "rule_version": "synthetic-fault", "details": "Synthetic false positive",
                }]
            if mutation == "confidence_only":
                changes["confidence"] = .50
            observations.append(replace(item, **changes))
        return tuple(observations)

    monkeypatch.setattr(extraction, "extract_observations", overcautious_extraction)
    report = runner()(dictionary)

    assert not report["passed"]
    assert report["counts"]["severe"]["escaped"] == 0
    assert report["fields"]["raw_value"]["correct"] == report["fields"]["raw_value"]["expected"]
    assert report["counts"]["normal"]["routing"] == {"numerator": 98, "denominator": 98, "rate": 1.0}
    assert any(failure["reason"] == "normal_control_degraded" for failure in report["failures"])


def test_normal_routing_has_frozen_supported_scope_and_complete_denominator(dictionary):
    report = runner()(dictionary)

    assert report["passed"], report["failures"]
    assert report["counts"]["normal"] == {
        "scope": "explicit_specimen_reviewed_unit_parser_controls",
        "minimum_confidence": .95,
        "total": 98, "assessed": 98, "preserved": 98,
        "routed": 0, "missing": 0, "unassessed": 0,
        "routing": {"numerator": 0, "denominator": 98, "rate": 0.0},
    }
    legacy = runner()(load_dictionary(DICTIONARIES / "v1.0.0.json"))
    assert legacy["counts"]["normal"]["unassessed"] == 98
    assert legacy["counts"]["normal"]["routing"] == {"numerator": 0, "denominator": 0, "rate": None}


@pytest.mark.parametrize(("confidence", "passed", "routed"), [(.95, True, 0), (.949, False, 98)])
def test_normal_controls_enforce_frozen_source_confidence_boundary(dictionary, monkeypatch, confidence, passed, routed):
    from apps.labs import extraction

    extract = extraction.extract_observations
    monkeypatch.setattr(extraction, "extract_observations", lambda pages, dictionary: tuple(
        replace(item, confidence=confidence) for item in extract(pages, dictionary)
    ))

    report = runner()(dictionary)

    assert report["passed"] is passed
    assert report["counts"]["normal"]["routing"]["numerator"] == routed
    assert report["counts"]["normal"]["routing"]["denominator"] == 98


def test_same_page_but_wrong_field_polygon_fails_location_recall(dictionary, monkeypatch):
    from apps.labs import extraction

    extract = extraction.extract_observations

    def misplaced_evidence(pages, dictionary):
        return tuple(
            replace(item, field_evidence={**item.field_evidence, "raw_value": item.field_evidence["raw_name"]})
            for item in extract(pages, dictionary)
        )

    monkeypatch.setattr(extraction, "extract_observations", misplaced_evidence)
    report = runner()(dictionary)

    assert report["fields"]["source_location"]["correct"] < report["fields"]["source_location"]["expected"]
    assert any(failure.get("field") == "source_location" for failure in report["failures"])


def test_corrupt_corpus_cannot_duplicate_cases_to_keep_total_counts(dictionary, monkeypatch, tmp_path):
    regression = importlib.import_module("apps.labs.regression")
    corpus = json.loads((DICTIONARIES / "phase-two-regression.json").read_text(encoding="utf-8"))
    corpus["targets"][0]["results"][1] = corpus["targets"][0]["results"][0]
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(json.dumps(corpus), encoding="utf-8")
    monkeypatch.setattr(regression, "_CORPUS_PATH", corrupt)

    with pytest.raises(ValueError, match="fixed_synthetic_corpus_invalid"):
        runner()(dictionary)


def test_baseline_compares_same_fixed_fields_and_reports_missing_values(dictionary):
    bad = replace(dictionary, indicators=tuple(item for item in dictionary.indicators if item.code != "LAB_HGB"))
    report = runner()(bad, baseline=dictionary)

    assert not report["passed"]
    assert "standard_code" in report["baseline"]["decreased_fields"]
    assert report["baseline"]["fields"]["standard_code"]["candidate_correct"] < report["baseline"]["fields"]["standard_code"]["baseline_correct"]
    assert any(failure["reason"] == "field_recall_decreased" for failure in report["failures"])


def test_only_exact_legacy_dictionary_can_leave_phase_two_targets_unassessed(dictionary):
    legacy = load_dictionary(DICTIONARIES / "v1.0.0.json")
    report = runner()(legacy)

    assert report["passed"], report["failures"]
    assert report["scope"] == "legacy"
    assert report["coverage"]["target_present"] == 55
    assert len(report["coverage"]["unassessed_target_codes"]) == 70
    assert report["counts"]["target_positive"]["assessed"] == 0
    assert report["counts"]["target_parser"]["assessed"] == 0
    assert report["counts"]["legacy_parser"]["assessed"] == 160
    assert report["counts"]["alias_context"]["unassessed"] == 23
    assert runner()(dictionary, baseline=legacy)["passed"]

    renamed = replace(legacy, version="unreviewed-next", content_hash="b" * 64)
    candidate = runner()(renamed)
    assert candidate["scope"] == "phase_two"
    assert not candidate["passed"]

    forged = replace(legacy, indicators=legacy.indicators[:-1])
    assert runner()(forged)["scope"] == "phase_two"


def test_fixed_corpus_is_independent_of_candidate_alias_and_source_files(dictionary, monkeypatch):
    original = Path.read_bytes

    def no_candidate_reread(path):
        assert path.name not in {"phase-two.json", "phase-two-coverage.json"}, "candidate cannot define expected cases"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", no_candidate_reread)
    assert runner()(dictionary)["passed"]


@pytest.mark.parametrize("line_ending", ["lf", "crlf"])
def test_exact_legacy_lf_and_crlf_bytes_retain_the_same_frozen_gate(line_ending, tmp_path):
    original_lf = (DICTIONARIES / "v1.0.0.json").read_bytes().replace(b"\r\n", b"\n")
    encoded = original_lf if line_ending == "lf" else original_lf.replace(b"\n", b"\r\n")
    path = tmp_path / "legacy.json"
    path.write_bytes(encoded)
    legacy = load_dictionary(path)

    assert legacy.content_hash == hashlib.sha256(encoded).hexdigest() == LEGACY_BYTE_HASHES[line_ending]
    report = runner()(legacy)
    assert report["scope"] == "legacy"
    assert report["passed"], report["failures"]
    assert report["counts"]["legacy_parser"]["assessed"] == 160
    assert report["counts"]["target_parser"]["unassessed"] == 373


def test_semantically_identical_unfrozen_legacy_bytes_do_not_receive_an_exemption(tmp_path):
    encoded = (DICTIONARIES / "v1.0.0.json").read_bytes().replace(b"\r\n", b"\n") + b" "
    path = tmp_path / "legacy-with-space.json"
    path.write_bytes(encoded)
    legacy = load_dictionary(path)
    regression = importlib.import_module("apps.labs.regression")
    corpus = json.loads((DICTIONARIES / "phase-two-regression.json").read_text(encoding="utf-8"))

    assert legacy.content_hash not in LEGACY_BYTE_HASHES.values()
    assert regression._definition_digest(legacy) == corpus["legacy_identity"]["definition_sha256"]
    report = runner()(legacy)
    assert report["scope"] == "phase_two"
    assert not report["passed"]


@pytest.mark.parametrize("byte_hash", LEGACY_BYTE_HASHES.values(), ids=LEGACY_BYTE_HASHES.keys())
@pytest.mark.parametrize("change", ["version", "definition"])
def test_either_allowed_byte_hash_still_requires_the_exact_legacy_definition(byte_hash, change):
    legacy = replace(load_dictionary(DICTIONARIES / "v1.0.0.json"), content_hash=byte_hash)
    changes = {"version": "unreviewed-next"} if change == "version" else {"indicators": legacy.indicators[:-1]}
    report = runner()(replace(legacy, **changes))

    assert report["scope"] == "phase_two"
    assert not report["passed"]
