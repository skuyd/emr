import json
import re

import pytest

from apps.labs.dictionary import (
    CapabilityLevel,
    DictionaryError,
    IndicatorDictionary,
    default_dictionary,
    load_dictionary,
)


def test_v1_dictionary_is_versioned_hashed_and_has_exactly_125_evidenced_indicators():
    dictionary = default_dictionary()

    assert dictionary.version == "1.0.0"
    assert re.fullmatch(r"[0-9a-f]{64}", dictionary.content_hash)
    assert len(dictionary.indicators) == 125
    assert len({item.code for item in dictionary.indicators}) == 125
    assert len({item.standard_name for item in dictionary.indicators}) == 125
    assert sum(item.category != "MOLECULAR_GENE" for item in dictionary.indicators) == 75
    assert sum(item.category == "MOLECULAR_GENE" for item in dictionary.indicators) == 50
    assert all(item.evidence_context_hashes for item in dictionary.indicators)
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", digest)
        for item in dictionary.indicators
        for digest in item.evidence_context_hashes
    )


def test_exact_alias_and_explicit_ocr_variant_matching_never_fuzzy_guesses():
    dictionary = default_dictionary()

    assert dictionary.match("WBC").code == "LAB_WBC"
    assert dictionary.match("  ＷＢＣ  ").code == "LAB_WBC"
    assert dictionary.match("甘油三脂").code == "LAB_TG"
    assert dictionary.match("MCHC 平均血红蛋白浓").code == "LAB_MCHC"
    assert dictionary.match("BRCA1").code == "GENE_BRCA1"
    assert dictionary.match("BRCA1").capability_level == CapabilityLevel.SEARCH_ONLY
    assert dictionary.match("RBC情") is None
    assert dictionary.match("可能是白细胞的叙述") is None
    assert dictionary.match("") is None


def test_molecular_gene_entries_are_unitless_and_cannot_be_treated_as_stable_numeric_items():
    genes = [item for item in default_dictionary().indicators if item.category == "MOLECULAR_GENE"]

    assert len(genes) == 50
    assert all(item.capability_level == CapabilityLevel.SEARCH_ONLY for item in genes)
    assert all(item.unit_forms == () for item in genes)


def test_dictionary_resource_contains_no_source_alias_path_or_raw_medical_context():
    path = default_dictionary().source_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "source_file_hash" not in serialized
    assert "source_path" not in serialized
    assert "raw_name" not in serialized
    assert "raw_value" not in serialized
    assert "context_text" not in serialized
    assert "示例" not in serialized


def test_loader_rejects_ambiguous_aliases_instead_of_choosing_an_indicator(tmp_path):
    payload = {
        "schema_version": "1.0",
        "dictionary_version": "test-1",
        "generated_from": {
            "candidate_report_sha256": "a" * 64,
            "hgnc_dataset_sha256": "b" * 64,
            "hgnc_dataset_url": "https://example.invalid/reference",
            "hgnc_checked_on": "2026-08-31",
        },
        "indicators": [
            {
                "aliases": ["SHARED"],
                "capability_level": "STABLE",
                "category": "LABORATORY",
                "code": "LAB_ONE",
                "evidence_context_hashes": ["c" * 64],
                "ocr_variants": [],
                "standard_name": "合成指标一",
                "unit_forms": [],
            },
            {
                "aliases": ["shared"],
                "capability_level": "EXPLORATORY",
                "category": "LABORATORY",
                "code": "LAB_TWO",
                "evidence_context_hashes": ["d" * 64],
                "ocr_variants": [],
                "standard_name": "合成指标二",
                "unit_forms": [],
            },
        ],
    }
    path = tmp_path / "ambiguous.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DictionaryError, match="ambiguous_indicator_alias"):
        load_dictionary(path)


def test_dictionary_value_objects_are_immutable_and_default_loader_is_cached():
    dictionary = default_dictionary()

    assert default_dictionary() is dictionary
    assert isinstance(dictionary, IndicatorDictionary)
    with pytest.raises(AttributeError):
        dictionary.indicators[0].code = "CHANGED"
