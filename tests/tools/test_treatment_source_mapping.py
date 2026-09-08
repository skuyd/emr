import pytest


def mapper(*texts):
    from tools.treatment_source_mapping import SourceMapper
    return SourceMapper({(1, 1): [{"text": text, "reading_order": i} for i, text in enumerate(texts)]})


def test_mapping_keeps_original_region_unicode_coordinates_across_ocr_line_wraps():
    result = mapper("2024-01-01 给予", "方案甲 化疗。").map_quote(1, 1, "2024-01-01给予方案甲化疗。")
    assert result["status"] == "MATCHED" and result["match_count"] == 1
    assert result["region_numbers"] == [1, 2]
    assert result["spans"] == [{"region": 1, "start": 0, "end": 13}, {"region": 2, "start": 0, "end": 7}]


def test_local_fact_offsets_are_mapped_through_the_unique_full_source_not_as_ocr_offsets():
    source = "说明：2024-01-01给予方案甲化疗。"
    index = mapper("页头", source, "其他：2024-01-01给予方案甲化疗。")
    raw = source[3:]
    assert index.map_quote(1, 1, raw)["status"] == "UNJUDGED"
    result = index.map_quote(1, 1, raw, context=source, start=3, end=len(source))
    assert result["status"] == "MATCHED" and result["context_match_count"] == 1
    assert result["region_numbers"] == [2] and result["spans"][0]["start"] == 3


@pytest.mark.parametrize("raw,reason", [("重复治疗", "ambiguous_quote"), ("不存在", "quote_not_found")])
def test_nonunique_or_missing_quotes_remain_unjudged(raw, reason):
    result = mapper("重复治疗", "重复治疗").map_quote(1, 1, raw)
    assert result["status"] == "UNJUDGED" and result["reason"] == reason
    assert result["spans"] == []


def test_invalid_fact_slice_cannot_fall_back_to_a_matching_word_elsewhere():
    result = mapper("说明：给予方案甲治疗。").map_quote(1, 1, "方案甲", context="说明：给予方案甲治疗。", start=0, end=3)
    assert result["status"] == "UNJUDGED" and result["reason"] == "source_slice_mismatch"


def test_frozen_gold_region_numbers_are_verified_against_their_own_quote():
    index = mapper("方案甲", "方案甲")
    result = index.map_quote(1, 1, "方案甲", regions=[2])
    assert result["status"] == "MATCHED" and result["region_numbers"] == [2]
    assert index.map_quote(1, 1, "方案乙", regions=[2])["status"] == "UNJUDGED"


def test_mapping_does_not_normalize_punctuation_or_replace_original_dates():
    assert mapper("2024-01-01治疗").map_quote(1, 1, "2024/01/01治疗")["status"] == "UNJUDGED"
