from dataclasses import dataclass

from django.db.models import Prefetch

from apps.labs.models import LabObservation
from apps.labs.trends import eligible_trend_codes
from apps.processing.models import DatePrecision, DocumentType, OcrBlock, ParsingVersion

from .models import Document


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str


def format_document_date(value, precision):
    if value is None or precision == DatePrecision.UNKNOWN:
        return "日期未识别"
    if precision == DatePrecision.YEAR:
        return f"{value.year}年"
    if precision == DatePrecision.MONTH:
        return f"{value.year}年{value.month}月"
    return f"{value.year}年{value.month}月{value.day}日"


def document_detail_queryset(patient):
    observations = (
        LabObservation.objects.select_related("document_page", "evidence", "evidence__document_page")
        .order_by("document_page__page_number", "reading_order", "pk")
    )
    blocks = OcrBlock.objects.select_related("document_page").order_by(
        "document_page__page_number", "reading_order", "pk"
    )
    versions = (
        ParsingVersion.objects.filter(active=True)
        .select_related("document_summary")
        .prefetch_related(
            Prefetch("lab_observations", queryset=observations, to_attr="detail_observations"),
            Prefetch("ocr_blocks", queryset=blocks, to_attr="detail_ocr_blocks"),
        )
    )
    return (
        Document.objects.filter(patient=patient, deleted_at__isnull=True)
        .prefetch_related(Prefetch("parsing_versions", queryset=versions, to_attr="detail_versions"))
    )


def _ocr_pages(version):
    grouped = []
    for block in version.detail_ocr_blocks if version is not None else ():
        page_number = block.document_page.page_number
        if not grouped or grouped[-1][0] != page_number:
            grouped.append((page_number, []))
        grouped[-1][1].append(block.text)
    return tuple(OcrPage(page_number=page, text="\n".join(lines)) for page, lines in grouped)


def document_detail_context(document):
    version = document.detail_versions[0] if document.detail_versions else None
    summary = getattr(version, "document_summary", None) if version is not None else None
    document_type = summary.document_type if summary is not None else DocumentType.UNKNOWN
    precision = summary.date_precision if summary is not None else DatePrecision.UNKNOWN
    document_date = summary.document_date if summary is not None else None
    observations = tuple(version.detail_observations) if version is not None else ()
    trend_codes = eligible_trend_codes(document.patient, (item.standard_code for item in observations))
    for observation in observations:
        observation.show_standard_name = (
            not observation.standard_code.startswith("CANDIDATE_")
            and observation.standard_name.strip().casefold() != observation.raw_name.strip().casefold()
        )
        observation.show_trend = observation.standard_code in trend_codes
    return {
        "document": document,
        "active_version": version,
        "document_type_label": DocumentType(document_type).label,
        "document_date_label": format_document_date(document_date, precision),
        "institution": summary.institution_raw.strip() if summary is not None else "",
        "observations": observations,
        "ocr_pages": _ocr_pages(version),
        "current_section": "records",
    }
