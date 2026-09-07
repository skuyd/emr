from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Prefetch

from apps.labs.models import LabObservation
from apps.labs.presentation import review_status, summarize_issues
from apps.labs.quality import MIN_OBSERVATION_CONFIDENCE, MIN_STANDARD_NAME_CONFIDENCE, unreliable_selected_date_q
from apps.labs.trends import eligible_trend_codes
from apps.labs.readmodels import checked_reference, effective_document_date, effective_rows, reconciliation_rows, visible_observation
from apps.labs.validation import validate_observation
from apps.processing.models import DatePrecision, DocumentMetadataCandidate, DocumentType, OcrBlock, ParsingVersion
from apps.processing.reprocessing import quality_refresh_required
from apps.processing.material_review import material_state

from .models import Document, DocumentStatus
from .titles import document_title


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
        LabObservation.objects.all()
        .select_related("document_page", "evidence", "evidence__document_page")
        .order_by("document_page__page_number", "reading_order", "pk")
    )
    blocks = OcrBlock.objects.select_related("document_page").order_by(
        "document_page__page_number", "reading_order", "pk"
    )
    versions = (
        ParsingVersion.objects.filter(active=True)
        .annotate(date_is_unreliable=Exists(DocumentMetadataCandidate.objects.filter(
            unreliable_selected_date_q(), parsing_version_id=OuterRef("pk"),
        )))
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
    if version is not None and version.date_is_unreliable:
        precision, document_date = DatePrecision.UNKNOWN, None
    observations = tuple(item for row in version.detail_observations if (item := visible_observation(row)) is not None) if version is not None else ()
    document_date, precision = effective_document_date(observations, document_date, precision)
    trend_codes = eligible_trend_codes(document.patient, (item.standard_code for item in observations))
    previous = effective_rows(document.patient, include_uncertain=True) if observations else ()
    for observation in observations:
        observation.show_standard_name = (
            not observation.standard_code.startswith("CANDIDATE_")
            and observation.evidence.confidence is not None and observation.evidence.confidence >= MIN_STANDARD_NAME_CONFIDENCE
            and observation.standard_name.strip().casefold() != observation.raw_name.strip().casefold()
        )
        observation.show_trend = observation.standard_code in trend_codes
        issues = validate_observation(observation, previous=previous)
        observation.display_issues = tuple({item["code"]: item for item in issues}.values())
        observation.review_status = review_status(observation)
        observation.reference_comparison = checked_reference(observation, issues)
    status_key = {
        DocumentStatus.PROCESSING: "processing",
        DocumentStatus.ORGANIZED: "organized",
        DocumentStatus.ORIGINAL_ONLY: "original",
        DocumentStatus.PROCESSING_FAILED: "failed",
    }.get(document.status, "processing")
    needs_quality_reprocessing = quality_refresh_required(document, version)
    return {
        "document": document,
        "document_title": document_title(
            document, version,
            blocks=version.detail_ocr_blocks if version is not None else (),
            observations=observations,
        ),
        "active_version": version,
        "material": material_state(document, version),
        "material_decisions": document.material_decisions.order_by("-sequence")[:20],
        "document_type_label": DocumentType(document_type).label,
        "document_type_code": DocumentType(document_type).value,
        "document_date_label": format_document_date(document_date, precision),
        "institution": summary.institution_raw.strip() if summary is not None else "",
        "observations": observations,
        "quality_summary": summarize_issues(observations),
        "quality_observation_count": sum(bool(row.display_issues) for row in observations),
        "reconciliation": reconciliation_rows(version, observations) if version else (),
        "available_versions": document.parsing_versions.filter(status="PUBLISHED").order_by("-created_at"),
        "ocr_pages": _ocr_pages(version),
        "status_key": status_key,
        "status_label": document.get_status_display(),
        "current_section": "records",
        "needs_quality_reprocessing": needs_quality_reprocessing,
        "can_reprocess": document.status == DocumentStatus.PROCESSING_FAILED or needs_quality_reprocessing,
    }
