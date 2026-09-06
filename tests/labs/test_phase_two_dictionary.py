import json
from pathlib import Path

import pytest

from apps.labs.dictionary import (
    CapabilityLevel,
    DictionaryError,
    IndicatorDefinition,
    IndicatorDictionary,
    load_dictionary,
    normalize_indicator_alias,
)


ROOT = Path(__file__).resolve().parents[2]
PHASE_TWO_PATH = ROOT / "apps" / "labs" / "dictionaries" / "phase-two.json"
COVERAGE_PATH = ROOT / "apps" / "labs" / "dictionaries" / "phase-two-coverage.json"
V1_PATH = ROOT / "apps" / "labs" / "dictionaries" / "v1.0.0.json"


@pytest.mark.django_db
def test_new_runs_default_to_phase_two_while_historical_dictionary_remains_loadable():
    from apps.labs.dictionary import current_dictionary, default_dictionary, dictionary_for_version
    assert current_dictionary().version == 'phase-two-1'
    assert current_dictionary().match('葡萄糖', specimen='URINE').code == 'LAB_URINE_GLUCOSE'
    assert default_dictionary().version == '1.0.0'
    assert dictionary_for_version('1.0.0').content_hash == default_dictionary().content_hash

EXPECTED_ROWS = (
    ("TIER_1", "CBC", "WBC, NEUT#, NEUT%, LYMPH#, LYMPH%, MONO#, MONO%, EO#, EO%, BASO#, BASO%, RBC, HGB, HCT, MCV, MCH, MCHC, RDW-CV, PLT, MPV, PDW, PCT"),
    ("TIER_1", "LIVER_FUNCTION", "ALT, AST, ALP, GGT, TBIL, DBIL, IBIL, TP, ALB, GLB, A/G, LDH"),
    ("TIER_1", "RENAL_FUNCTION", "Cr, BUN, UA, eGFR, Urea/Cr, Cys-C"),
    ("TIER_1", "ELECTROLYTES", "K, Na, Cl, Ca(校正钙), Mg, P"),
    ("TIER_1", "COAGULATION", "PT, APTT, INR, D-二聚体"),
    ("TIER_1", "INFLAMMATION", "CRP, ESR"),
    ("TIER_1", "GLUCOSE_METABOLISM", "空腹血糖, HbA1c"),
    ("TIER_1", "NUTRITION", "前白蛋白 PA"),
    ("TIER_1", "SERUM_IRON", "血清铁"),
    ("TIER_1", "TUMOR_MARKER", "CEA, AFP, CA125, CA153, CA199, CA724, CA242, CA50, CYFRA21-1, NSE, ProGRP, SCC, PSA, fPSA"),
    ("TIER_2", "CARDIAC_MARKER", "cTnI, CK-MB, 肌红蛋白, BNP, NT-proBNP"),
    ("TIER_2", "THYROID_FUNCTION", "TSH, FT3, FT4, T3, T4"),
    ("TIER_2", "LIPIDS", "TC, TG, LDL-C, HDL-C"),
    ("TIER_2", "IRON_METABOLISM", "铁蛋白, 转铁蛋白, 转铁蛋白饱和度"),
    ("TIER_2", "PANCREATIC_ENZYME", "淀粉酶 AMS, 脂肪酶 LPS"),
    ("TIER_2", "URINALYSIS", "蛋白, 潜血, 白细胞, 红细胞, 葡萄糖, 酮体, 亚硝酸盐, 尿胆原, 比重, pH"),
    ("TIER_2", "STOOL", "隐血 OB, 红细胞, 白细胞"),
    ("TIER_2", "VIRAL_SCREEN", "HBsAg, 抗-HBs, HBeAg, 抗-HBe, 抗-HBc, Anti-HIV, TP-Ab, HCV-Ab"),
    ("TIER_2", "COAGULATION", "TT, FIB, FDP"),
    ("TIER_2", "GLUCOSE_METABOLISM", "C肽, β-羟丁酸"),
    ("TIER_2", "RENAL_FUNCTION", "尿微量白蛋白/肌酐比, β2-MG"),
    ("TIER_2", "INFLAMMATION", "PCT, IL-6"),
    ("TIER_2", "NUTRITION", "转铁蛋白 TRF, 视黄醇结合蛋白 RBP"),
    ("TIER_2", "OTHER", "乳酸, 同型半胱氨酸, 叶酸, 维生素B12"),
)


def _expected_source_rows():
    return [
        (tier, panel, label.strip())
        for tier, panel, labels in EXPECTED_ROWS
        for label in labels.split(",")
    ]


def _payload(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_v1_loader_remains_byte_compatible_and_new_fields_have_safe_defaults():
    before = V1_PATH.read_bytes()
    dictionary = load_dictionary(V1_PATH)

    assert V1_PATH.read_bytes() == before
    assert dictionary.version == "1.0.0"
    assert dictionary.match("WBC").code == "LAB_WBC"
    assert dictionary.match("WBC").specimen == ""
    assert dictionary.match("WBC").result_types == ()
    assert dictionary.match("WBC").tier == ""


def test_phase_two_artifact_preserves_every_existing_code_name_alias_and_ocr_variant():
    legacy = load_dictionary(V1_PATH)
    phase_two = load_dictionary(PHASE_TWO_PATH)
    phase_by_code = {item.code: item for item in phase_two.indicators}

    assert set(phase_by_code) >= {item.code for item in legacy.indicators}
    for previous in legacy.indicators:
        current = phase_by_code[previous.code]
        assert current.standard_name == previous.standard_name
        assert set(current.aliases) >= set(previous.aliases)
        assert set(current.ocr_variants) >= set(previous.ocr_variants)
        assert current.unit_forms == previous.unit_forms


def test_phase_two_coverage_transcribes_every_original_row_and_keeps_genes_separate():
    coverage = _payload(COVERAGE_PATH)
    actual_rows = [
        (row["tier"], row["panel"], row["original_label"])
        for row in coverage["original_rows"]
    ]

    assert coverage["schema_version"] == "1.0"
    assert actual_rows == _expected_source_rows()
    assert len(actual_rows) == 125
    assert coverage["summary"] == {
        "distinct_target_definitions": 125,
        "gene_definitions": 50,
        "original_rows": 125,
        "real_sample_status": "not_evaluated",
        "invalid_placeholder_context_cases": 0,
        "normalized_alias_collision_groups": 7,
        "normalized_alias_context_cases": 23,
        "synthetic_neighbor_indicator_cases": 112,
        "synthetic_negative_cases": 125,
        "synthetic_positive_cases": 125,
        "synthetic_shared_alias_cases": 13,
        "tier_1_rows": 70,
        "tier_2_rows": 55,
    }
    assert len(coverage["target_definitions"]) == 125
    assert len(coverage["genes"]) == 50
    assert not ({item["code"] for item in coverage["target_definitions"]} & {item["code"] for item in coverage["genes"]})
    assert all(item["real_sample_status"] == "not_evaluated" for item in coverage["target_definitions"])
    assert all(item["synthetic_positive"] and item["synthetic_negative"] for item in coverage["target_definitions"])


def test_duplicate_and_split_source_rows_are_explicit_without_silent_medical_derivation():
    rows = _payload(COVERAGE_PATH)["original_rows"]
    relationships = {row["original_label"]: row["relationship"] for row in rows if row["relationship"]["kind"] != "direct"}

    assert relationships == {
        "Ca(校正钙)": {
            "kind": "split",
            "codes": ["LAB_CA", "LAB_CORRECTED_CA"],
            "reason": "原始行将总钙缩写与校正钙名称并列；两者保持独立身份，且不计算校正钙。",
        },
        "转铁蛋白 TRF": {
            "kind": "duplicate",
            "codes": ["LAB_TRANSFERRIN"],
            "duplicates_original_row": "T2-IRON_METABOLISM-02",
        },
    }


def test_every_distinct_target_has_contextual_positive_and_negative_matching():
    dictionary = load_dictionary(PHASE_TWO_PATH)
    coverage = _payload(COVERAGE_PATH)

    for target in coverage["target_definitions"]:
        positive = target["synthetic_positive"]
        negative = target["synthetic_negative"]
        matched = dictionary.match(
            positive["raw_name"],
            specimen=positive["specimen"],
            panel=positive["panel"],
        )
        assert matched is not None, target["code"]
        assert matched.code == target["code"]
        negative_match = dictionary.match(
            negative["raw_name"],
            specimen=negative["specimen"],
            panel=negative["panel"],
        )
        actual_negative_code = negative_match.code if negative_match else None
        assert negative["kind"] in {"competing_indicator", "unresolved_ambiguity"}
        assert negative["specimen"] != "OTHER"
        assert actual_negative_code == negative["expected_code"], target["code"]
        assert actual_negative_code != target["code"], target["code"]


def test_every_normalized_alias_collision_has_independently_specified_context_cases():
    dictionary = load_dictionary(PHASE_TWO_PATH)
    expected = {
        "egfr": (
            ("EGFR", "", "", None),
            ("EGFR", "UNSPECIFIED", "MOLECULAR_GENE", "GENE_EGFR"),
            ("eGFR", "BLOOD", "RENAL_FUNCTION", "LAB_EGFR"),
        ),
        "glu": (
            ("GLU", "", "", None),
            ("GLU", "BLOOD", "GLUCOSE_METABOLISM", "LAB_FASTING_GLUCOSE"),
            ("GLU", "URINE", "URINALYSIS", "LAB_URINE_GLUCOSE"),
        ),
        "pct": (
            ("PCT", "", "", None),
            ("PCT", "BLOOD", "CBC", "LAB_PLATELETCRIT"),
            ("PCT", "BLOOD", "INFLAMMATION", "LAB_PCT"),
        ),
        "白细胞": (
            ("白细胞", "", "", None),
            ("白细胞", "BLOOD", "CBC", "LAB_WBC"),
            ("白细胞", "URINE", "URINALYSIS", "LAB_URINE_WBC"),
            ("白细胞", "STOOL", "STOOL", "LAB_STOOL_WBC"),
        ),
        "红细胞": (
            ("红细胞", "", "", None),
            ("红细胞", "BLOOD", "CBC", "LAB_RBC"),
            ("红细胞", "URINE", "URINALYSIS", "LAB_URINE_RBC"),
            ("红细胞", "STOOL", "STOOL", "LAB_STOOL_RBC"),
        ),
        "葡萄糖": (
            ("葡萄糖", "", "", None),
            ("葡萄糖", "BLOOD", "GLUCOSE_METABOLISM", "LAB_FASTING_GLUCOSE"),
            ("葡萄糖", "URINE", "URINALYSIS", "LAB_URINE_GLUCOSE"),
        ),
        "潜血": (
            ("潜血", "", "", None),
            ("潜血", "URINE", "URINALYSIS", "LAB_URINE_OCCULT_BLOOD"),
            ("潜血", "STOOL", "STOOL", "LAB_STOOL_OCCULT_BLOOD"),
        ),
    }
    aliases = {}
    for item in dictionary.indicators:
        for raw_alias in (item.standard_name, *item.aliases, *item.ocr_variants):
            aliases.setdefault(normalize_indicator_alias(raw_alias), set()).add(item.code)

    assert {alias for alias, codes in aliases.items() if len(codes) > 1} == set(expected)
    for cases in expected.values():
        for raw_name, specimen, panel, expected_code in cases:
            match = dictionary.match(raw_name, specimen=specimen, panel=panel)
            assert (match.code if match else None) == expected_code


def test_ambiguous_pct_glucose_and_body_material_aliases_require_context():
    dictionary = load_dictionary(PHASE_TWO_PATH)

    assert dictionary.match("PCT") is None
    assert dictionary.match("PCT", panel="CBC").code == "LAB_PLATELETCRIT"
    assert dictionary.match("PCT", panel="INFLAMMATION").code == "LAB_PCT"
    assert dictionary.match("葡萄糖") is None
    assert dictionary.match("葡萄糖", specimen="URINE").code == "LAB_URINE_GLUCOSE"
    assert dictionary.match("葡萄糖", specimen="BLOOD").code == "LAB_FASTING_GLUCOSE"
    assert dictionary.match("白细胞") is None
    assert dictionary.match("白细胞", specimen="BLOOD", panel="CBC").code == "LAB_WBC"
    assert dictionary.match("白细胞", specimen="URINE").code == "LAB_URINE_WBC"
    assert dictionary.match("白细胞", specimen="STOOL").code == "LAB_STOOL_WBC"


def test_total_and_corrected_calcium_never_silently_match_each_other():
    dictionary = load_dictionary(PHASE_TWO_PATH)

    assert dictionary.match("钙", specimen="BLOOD", panel="ELECTROLYTES").code == "LAB_CA"
    assert dictionary.match("校正钙", specimen="BLOOD", panel="ELECTROLYTES").code == "LAB_CORRECTED_CA"
    assert "校正钙" not in dictionary.match("钙", specimen="BLOOD", panel="ELECTROLYTES").aliases
    assert "钙" not in dictionary.match("校正钙", specimen="BLOOD", panel="ELECTROLYTES").aliases


def test_phase_two_entries_declare_tier_specimen_and_distinct_result_semantics():
    dictionary = load_dictionary(PHASE_TWO_PATH)
    target_items = [item for item in dictionary.indicators if item.tier in {"TIER_1", "TIER_2"}]

    assert len(target_items) == 125
    assert sum(item.tier == "TIER_1" for item in target_items) == 71
    assert sum(item.tier == "TIER_2" for item in target_items) == 54
    assert all(item.specimen for item in target_items)
    assert all(item.result_types for item in target_items)
    assert {kind for item in target_items for kind in item.result_types} == {
        "comparator",
        "numeric",
        "qualitative",
        "semi_quantitative",
        "status",
    }
    assert not any(hasattr(item, "default_reference_range") for item in target_items)
    assert not any(hasattr(item, "conversion_rules") for item in target_items)


def test_phase_two_loader_rejects_unknown_fields_and_context_collisions(tmp_path):
    payload = _payload(PHASE_TWO_PATH)
    payload["indicators"][0]["unreviewed_default_range"] = "invented"
    malformed = tmp_path / "unknown-field.json"
    malformed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DictionaryError, match="invalid_indicator_record"):
        load_dictionary(malformed)

    payload = _payload(PHASE_TWO_PATH)
    first = payload["indicators"][0]
    second = payload["indicators"][1]
    second["aliases"] = [first["aliases"][0]]
    second["specimen"] = first["specimen"]
    second["category"] = first["category"]
    malformed = tmp_path / "context-collision.json"
    malformed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DictionaryError, match="ambiguous_indicator_alias"):
        load_dictionary(malformed)


@pytest.mark.parametrize("contextless_first", [False, True])
def test_alias_collision_with_default_context_is_rejected_in_both_orders(contextless_first):
    contextful = IndicatorDefinition(
        code="LAB_CONTEXTUAL",
        standard_name="上下文指标",
        aliases=("SHARED",),
        ocr_variants=(),
        unit_forms=(),
        category="CBC",
        capability_level=CapabilityLevel.STABLE,
        evidence_context_hashes=("a" * 64,),
        specimen="BLOOD",
        result_types=("numeric",),
        tier="TIER_1",
    )
    contextless = IndicatorDefinition(
        code="LAB_CONTEXTLESS",
        standard_name="默认指标",
        aliases=("shared",),
        ocr_variants=(),
        unit_forms=(),
        category="LABORATORY",
        capability_level=CapabilityLevel.STABLE,
        evidence_context_hashes=("b" * 64,),
    )
    indicators = (contextless, contextful) if contextless_first else (contextful, contextless)

    with pytest.raises(DictionaryError, match="ambiguous_indicator_alias"):
        IndicatorDictionary(
            version="test-1",
            content_hash="c" * 64,
            indicators=indicators,
            source_path=PHASE_TWO_PATH,
            generated_from=(),
        )
