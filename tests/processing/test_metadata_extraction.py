from datetime import date

from apps.processing.metadata import extract_document_metadata
from apps.processing.models import DatePrecision, DocumentType, MetadataKind
from apps.processing.value_objects import OcrPage, OcrRegion


def _region(text, order, top):
    return OcrRegion(
        text,
        ((0.05, top), (0.90, top), (0.90, top + 0.04), (0.05, top + 0.04)),
        0.98,
        reading_order=order,
    )


def _page(*texts):
    return OcrPage(
        1,
        1000,
        1400,
        tuple(_region(text, index, 0.05 * index) for index, text in enumerate(texts, start=1)),
        "fixture",
        "1.0",
    )


def test_lab_summary_prefers_sampling_date_and_preserves_all_candidates_with_evidence():
    metadata = extract_document_metadata(
        (_page("合成医学检验中心 检验报告", "采样日期：2026-08-20", "报告日期：2026-08-21"),),
        observation_count=3,
    )

    assert metadata.document_type == DocumentType.LAB
    assert metadata.document_date == date(2026, 8, 20)
    assert metadata.date_precision == DatePrecision.DAY
    assert metadata.document_date_raw == "采样日期：2026-08-20"
    assert metadata.institution_raw == "合成医学检验中心 检验报告"
    dates = [candidate for candidate in metadata.candidates if candidate.kind == MetadataKind.DOCUMENT_DATE]
    assert len(dates) == 2
    assert sum(candidate.selected for candidate in dates) == 1
    assert all(candidate.page_number == 1 and candidate.region for candidate in dates)


def test_same_priority_conflicting_dates_remain_unknown_without_user_input():
    metadata = extract_document_metadata(
        (_page("检验报告", "采样日期 2026-08-20", "检查日期 2026-08-19"),),
        observation_count=1,
    )

    assert metadata.document_date is None
    assert metadata.date_precision == DatePrecision.UNKNOWN
    assert metadata.document_date_raw == ""
    dates = [candidate for candidate in metadata.candidates if candidate.kind == MetadataKind.DOCUMENT_DATE]
    assert len(dates) == 2
    assert all(candidate.selected is False for candidate in dates)


def test_month_precision_is_retained_and_never_presented_as_an_exact_day_in_metadata():
    metadata = extract_document_metadata((_page("影像报告", "检查日期 2026年7月"),))

    assert metadata.document_type == DocumentType.IMAGING
    assert metadata.document_date == date(2026, 7, 1)
    assert metadata.date_precision == DatePrecision.MONTH
    assert metadata.document_date_raw == "检查日期 2026年7月"


def test_nonempty_unclassified_ocr_is_other_while_empty_ocr_is_unknown():
    other = extract_document_metadata((_page("一份无法可靠分类的合成资料"),))
    empty = extract_document_metadata((OcrPage(1, 100, 100, (), "fixture", "1.0"),))

    assert other.document_type == DocumentType.OTHER
    assert empty.document_type == DocumentType.UNKNOWN
