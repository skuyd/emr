import pytest

from apps.labs.candidates import extract_lab_candidates
from apps.processing.value_objects import OcrPage, OcrRegion
from tools.sample_dictionary.normalize import is_rejected_candidate_name, normalize_candidate_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  白细胞　计数  ", "白细胞计数"),
        ("ＷＢＣ", "WBC"),
        ("超敏 C 反应蛋白 hs-CRP", "超敏C反应蛋白 hs-CRP"),
        ("白细胞计数 4.20 10^9/L", "白细胞计数"),
        ("C反应蛋白：阳性", "C反应蛋白"),
        ("* 03. 血红蛋白 ↓", "血红蛋白"),
        ("14★MON# 单核细胞计数", "MON# 单核细胞计数"),
    ],
)
def test_candidate_normalization_nfkc_whitespace_and_result_stripping(raw, expected):
    assert normalize_candidate_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "项目名称",
        "检验项目 结果 单位 参考范围",
        "患者姓名",
        "性别 男",
        "报告日期 2026-08-30",
        "某某医院检验科",
        "身份证号",
        "条码号",
        "姓名:合成姓名 病人类型:住院 科室:检验科 C反应蛋白",
        "患者第2周期治疗后出现骨髓抑制,中性粒细胞计数下降",
        "本检测项目通过对组织和白细胞对照进行分析",
        "周围少许渗出。白细胞计数正常",
    ],
)
def test_headers_demographics_identifiers_dates_and_institutions_are_rejected(raw):
    assert is_rejected_candidate_name(raw) is True


def _region(text, polygon, order):
    return OcrRegion(text, polygon, 0.98, reading_order=order)


def test_table_row_heuristic_requires_name_and_result_and_keeps_only_hashed_context():
    page = OcrPage(
        page_number=2,
        width=1000,
        height=1400,
        provider="fixture",
        provider_version="1.0",
        regions=(
            _region("检验项目", ((0.05, 0.05), (0.25, 0.05), (0.25, 0.08), (0.05, 0.08)), 1),
            _region("结果", ((0.45, 0.05), (0.55, 0.05), (0.55, 0.08), (0.45, 0.08)), 2),
            _region("白细胞计数 WBC", ((0.05, 0.20), (0.35, 0.20), (0.35, 0.23), (0.05, 0.23)), 3),
            _region("4.20", ((0.45, 0.20), (0.55, 0.20), (0.55, 0.23), (0.45, 0.23)), 4),
            _region("10^9/L", ((0.60, 0.20), (0.72, 0.20), (0.72, 0.23), (0.60, 0.23)), 5),
            _region("患者姓名", ((0.05, 0.30), (0.25, 0.30), (0.25, 0.33), (0.05, 0.33)), 6),
            _region("合成姓名", ((0.45, 0.30), (0.60, 0.30), (0.60, 0.33), (0.45, 0.33)), 7),
            _region("备注信息", ((0.05, 0.40), (0.25, 0.40), (0.25, 0.43), (0.05, 0.43)), 8),
        ),
    )

    candidates = extract_lab_candidates((page,), "a" * 64)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.raw_name == "白细胞计数 WBC"
    assert candidate.normalized_name == "白细胞计数 WBC"
    assert candidate.page == 2
    assert candidate.source_file_hash == "a" * 64
    assert len(candidate.context_hash) == 64
    assert "合成姓名" not in repr(candidate)


def test_status_result_is_accepted_but_never_misread_as_part_of_name():
    page = OcrPage(
        1,
        100,
        100,
        (
            _region("乙肝表面抗原", ((0.1, 0.2), (0.4, 0.2), (0.4, 0.3), (0.1, 0.3)), 1),
            _region("阴性", ((0.6, 0.2), (0.8, 0.2), (0.8, 0.3), (0.6, 0.3)), 2),
        ),
        "fixture",
        "1.0",
    )

    assert [candidate.normalized_name for candidate in extract_lab_candidates((page,), "b" * 64)] == [
        "乙肝表面抗原"
    ]
