from apps.labs.extraction import extract_observations
from apps.labs.models import CapabilityLevel, ResultType
from apps.processing.value_objects import OcrPage, OcrRegion


def _region(text, left, right, order, *, top=0.2, bottom=0.24, confidence=0.98):
    return OcrRegion(
        text,
        ((left, top), (right, top), (right, bottom), (left, bottom)),
        confidence,
        reading_order=order,
    )


def _page(*regions):
    return OcrPage(1, 1000, 1400, regions, "fixture", "1.0")


def test_numeric_observation_preserves_raw_value_unit_range_flag_and_evidence_region():
    observations = extract_observations(
        (
            _page(
                _region("WBC 白细胞", 0.05, 0.30, 1),
                _region("4.20", 0.38, 0.48, 2),
                _region("10^9/L", 0.52, 0.64, 3),
                _region("3.50-9.50", 0.68, 0.84, 4),
                _region("H", 0.88, 0.91, 5),
            ),
        )
    )

    assert len(observations) == 1
    item = observations[0]
    assert item.raw_name == "WBC 白细胞"
    assert item.standard_code == "LAB_WBC"
    assert item.standard_name == "白细胞计数"
    assert item.raw_value == "4.20"
    assert item.result_type == ResultType.NUMERIC
    assert item.raw_unit == "10^9/L"
    assert item.reference_range_raw == "3.50-9.50"
    assert item.report_flag_raw == "H"
    assert item.dictionary_version == "1.0.0"
    assert item.capability_level == CapabilityLevel.STABLE
    assert item.region == ((0.05, 0.2), (0.91, 0.2), (0.91, 0.24), (0.05, 0.24))
    assert item.confidence == 0.98


def test_comparator_status_and_semi_quantitative_results_are_never_coerced_to_plain_numbers():
    pages = (
        _page(
            _region("C反应蛋白", 0.05, 0.30, 1, top=0.10, bottom=0.14),
            _region("<0.5", 0.40, 0.50, 2, top=0.10, bottom=0.14),
            _region("mg/L", 0.55, 0.64, 3, top=0.10, bottom=0.14),
            _region("乙型肝炎表面抗原", 0.05, 0.35, 4, top=0.20, bottom=0.24),
            _region("未检出", 0.45, 0.58, 5, top=0.20, bottom=0.24),
            _region("自定义定性项目", 0.05, 0.35, 6, top=0.30, bottom=0.34),
            _region("++", 0.45, 0.52, 7, top=0.30, bottom=0.34),
        ),
    )

    observations = extract_observations(pages)

    assert [(item.raw_value, item.result_type) for item in observations] == [
        ("<0.5", ResultType.COMPARATOR),
        ("未检出", ResultType.STATUS),
        ("++", ResultType.SEMI_QUANTITATIVE),
    ]
    assert all(item.raw_value != "0" for item in observations[1:])


def test_numeric_row_without_an_explicit_unit_is_rejected_unless_dictionary_defines_unitless_result():
    page = _page(
        _region("自定义数值项目", 0.05, 0.30, 1, top=0.10, bottom=0.14),
        _region("12.3", 0.40, 0.50, 2, top=0.10, bottom=0.14),
        _region("INR", 0.05, 0.30, 3, top=0.20, bottom=0.24),
        _region("1.02", 0.40, 0.50, 4, top=0.20, bottom=0.24),
    )

    observations = extract_observations((page,))

    assert [(item.standard_code, item.raw_value, item.raw_unit) for item in observations] == [
        ("LAB_INR", "1.02", "")
    ]


def test_unmapped_but_well_structured_result_gets_stable_candidate_code_and_search_only_capability():
    page = _page(
        _region("自定义酶活性", 0.05, 0.30, 1),
        _region("12.30", 0.40, 0.50, 2),
        _region("U/L", 0.55, 0.64, 3),
    )

    first = extract_observations((page,))[0]
    second = extract_observations((page,))[0]

    assert first.standard_code == second.standard_code
    assert first.standard_code.startswith("CANDIDATE_")
    assert first.standard_name == "自定义酶活性"
    assert first.capability_level == CapabilityLevel.SEARCH_ONLY


def test_near_match_is_not_silently_mapped_to_dictionary_indicator():
    page = _page(
        _region("RBC情", 0.05, 0.30, 1),
        _region("4.20", 0.40, 0.50, 2),
        _region("10^12/L", 0.55, 0.67, 3),
    )

    observation = extract_observations((page,))[0]

    assert observation.standard_code.startswith("CANDIDATE_")
    assert observation.standard_code != "LAB_RBC"
