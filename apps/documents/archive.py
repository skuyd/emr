import re
from dataclasses import dataclass
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, DateField, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.urls import reverse

from apps.labs.models import LabObservation
from apps.labs.readmodels import effective_document_date, effective_rows, reconciliation_rows
from apps.labs.validation import validate_observation
from apps.labs.quality import MIN_OBSERVATION_CONFIDENCE, MIN_STANDARD_NAME_CONFIDENCE, unreliable_selected_date_q
from apps.processing.models import (
    DatePrecision,
    DocumentMetadataCandidate,
    DocumentSummary,
    DocumentType,
    OcrBlock,
    ParsingVersion,
)
from apps.processing.material_review import material_state

from .models import Document, DocumentStatus
from .titles import document_title, title_search_q, with_title_evidence


ARCHIVE_PAGE_SIZE = 20
MAX_SEARCH_LENGTH = 100


@dataclass(frozen=True)
class RecordCard:
    document: Document
    title: str
    detail_url: str
    original_url: str
    date_label: str
    date_value: str
    type_label: str
    institution: str
    status_key: str
    status_label: str
    observation_count: int
    snippet: str
    date_unknown: bool
    result_position: int
    material_label: str


@dataclass(frozen=True)
class RecordGroup:
    label: str
    cards: tuple[RecordCard, ...]


def _date_label(value, precision):
    if value is None or precision == DatePrecision.UNKNOWN:
        return "日期未识别"
    if precision == DatePrecision.YEAR:
        return f"{value.year}年"
    if precision == DatePrecision.MONTH:
        return f"{value.year}年{value.month}月"
    return f"{value.year}年{value.month}月{value.day}日"


def _date_value(value, precision):
    if value is None or precision == DatePrecision.UNKNOWN:
        return ""
    if precision == DatePrecision.YEAR:
        return f"{value.year:04d}"
    if precision == DatePrecision.MONTH:
        return f"{value.year:04d}-{value.month:02d}"
    return value.isoformat()


def _bounded_int(value, minimum, maximum):
    normalized = str(value or "").strip()
    if not re.fullmatch(r"\d{1,4}", normalized):
        return ""
    parsed = int(normalized)
    if not minimum <= parsed <= maximum:
        return ""
    return str(parsed)


def _date_search_q(query):
    normalized = query.strip().replace("年", "-").replace("月", "-").replace("日", "")
    normalized = normalized.replace("/", "-").replace(".", "-")
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", normalized)
    if match is None:
        return None
    year, month, day = (int(value) if value else None for value in match.groups())
    if year < 1900 or year > 2100:
        return None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    lookup = Q(archive_date__year=year)
    if month is not None:
        lookup &= Q(archive_date__month=month)
    if day is not None:
        lookup &= Q(archive_date__day=day)
    return lookup


def _active_version_queryset(query):
    queryset = with_title_evidence(ParsingVersion.objects.filter(active=True).select_related("document_summary"))
    if query:
        queryset = queryset.prefetch_related(
            Prefetch(
                "ocr_blocks",
                queryset=OcrBlock.objects.filter(text__icontains=query)
                .select_related("document_page")
                .order_by("document_page__page_number", "reading_order", "pk"),
                to_attr="archive_ocr_blocks",
            ),
            Prefetch(
                "lab_observations",
                queryset=LabObservation.objects.filter(
                    Q(raw_name__icontains=query)
                    | Q(standard_name__icontains=query, evidence__confidence__gte=MIN_STANDARD_NAME_CONFIDENCE)
                    | Q(raw_value__icontains=query)
                    | Q(raw_unit__icontains=query),
                    evidence__confidence__gte=MIN_OBSERVATION_CONFIDENCE,
                )
                .select_related("document_page", "evidence")
                .order_by("document_page__page_number", "reading_order", "pk"),
                to_attr="archive_observations",
            ),
        )
    return queryset


def _base_queryset(patient, query):
    unreliable_dates = DocumentMetadataCandidate.objects.filter(
        unreliable_selected_date_q(),
        parsing_version__document_id=OuterRef("pk"),
        parsing_version__active=True,
    )
    summaries = DocumentSummary.objects.filter(
        parsing_version__document_id=OuterRef("pk"),
        parsing_version__active=True,
    )
    observation_counts = (
        LabObservation.objects.filter(
            parsing_version__document_id=OuterRef("pk"),
            parsing_version__active=True,
            evidence__confidence__gte=MIN_OBSERVATION_CONFIDENCE,
        )
        .values("parsing_version__document_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return (
        Document.objects.filter(patient=patient, deleted_at__isnull=True)
        .annotate(archive_date_unreliable=Exists(unreliable_dates))
        .annotate(
            archive_date=Case(
                When(archive_date_unreliable=True, then=Value(None)),
                default=Subquery(summaries.values("document_date")[:1]),
                output_field=DateField(),
            ),
            archive_precision=Case(
                When(archive_date_unreliable=True, then=Value(DatePrecision.UNKNOWN)),
                default=Subquery(summaries.values("date_precision")[:1]),
                output_field=CharField(),
            ),
            archive_type=Subquery(summaries.values("document_type")[:1]),
            archive_institution=Subquery(summaries.values("institution_raw")[:1]),
            archive_observation_count=Coalesce(
                Subquery(observation_counts[:1], output_field=IntegerField()),
                Value(0),
            ),
        )
        .prefetch_related(
            Prefetch("parsing_versions", queryset=_active_version_queryset(query), to_attr="archive_versions")
        )
    )


def _apply_search(queryset, query):
    if not query:
        return queryset
    active = {"parsing_version__document_id": OuterRef("pk"), "parsing_version__active": True}
    ocr_matches = OcrBlock.objects.filter(**active, text__icontains=query)
    summary_matches = DocumentSummary.objects.filter(**active).filter(
        Q(document_date_raw__icontains=query) | Q(institution_raw__icontains=query)
    )
    observation_matches = LabObservation.objects.filter(
        **active, evidence__confidence__gte=MIN_OBSERVATION_CONFIDENCE,
    ).filter(
        Q(raw_name__icontains=query)
        | Q(standard_name__icontains=query, evidence__confidence__gte=MIN_STANDARD_NAME_CONFIDENCE)
        | Q(raw_value__icontains=query) | Q(raw_unit__icontains=query)
    )
    search = (
        Q(display_filename__icontains=query)
        | Q(Exists(ocr_matches)) | Q(Exists(summary_matches)) | Q(Exists(observation_matches))
        | title_search_q(query)
    )
    matching_types = [value for value, label in DocumentType.choices if query.casefold() in label.casefold()]
    matching_statuses = [value for value, label in DocumentStatus.choices if query.casefold() in label.casefold()]
    if matching_types:
        search |= Q(archive_type__in=matching_types)
        if DocumentType.UNKNOWN in matching_types:
            search |= Q(archive_type__isnull=True)
    if matching_statuses:
        search |= Q(status__in=matching_statuses)
    date_lookup = _date_search_q(query)
    if date_lookup is not None:
        search |= date_lookup
    return queryset.filter(search)


def _active_version(document):
    return document.archive_versions[0] if document.archive_versions else None


def _summary(version):
    return getattr(version, "document_summary", None) if version is not None else None


def _source_values(document, version, summary):
    values = [
        document.display_filename,
        document.get_status_display(),
        _date_label(document.archive_date, document.archive_precision),
    ]
    values.extend(getattr(document, "archive_clinical_texts", ()))
    if summary is not None:
        values.extend(
            (
                summary.get_document_type_display(),
                summary.document_date_raw,
                summary.institution_raw,
            )
        )
    if version is not None:
        values.extend(block.text for block in getattr(version, "archive_ocr_blocks", ()))
        for observation in getattr(version, "archive_observations", ()):
            if observation.evidence.confidence is not None and observation.evidence.confidence >= MIN_STANDARD_NAME_CONFIDENCE:
                values.append(observation.standard_name)
            values.extend(
                (
                    observation.raw_name,
                    observation.standard_code,
                    observation.raw_value,
                    observation.raw_unit,
                    observation.observation_date.isoformat() if observation.observation_date else "",
                )
            )
    return (" ".join(str(value).split()) for value in values if value)


def _matching_excerpt(document, version, summary, query, *, width=140):
    if not query:
        return ""
    folded_query = query.casefold()
    for source in _source_values(document, version, summary):
        index = source.casefold().find(folded_query)
        if index < 0:
            continue
        if len(source) <= width:
            return source
        start = max(0, index - width // 3)
        end = min(len(source), start + width)
        start = max(0, end - width)
        return f"{'…' if start else ''}{source[start:end]}{'…' if end < len(source) else ''}"
    return ""


def _card(document, query, result_position):
    version = _active_version(document)
    summary = _summary(version)
    precision = document.archive_precision or DatePrecision.UNKNOWN
    document_type = document.archive_type or DocumentType.UNKNOWN
    status_key = {
        DocumentStatus.PROCESSING: "processing",
        DocumentStatus.ORGANIZED: "organized",
        DocumentStatus.ORIGINAL_ONLY: "original",
        DocumentStatus.PROCESSING_FAILED: "failed",
    }.get(document.status, "original")
    return RecordCard(
        document=document,
        title=document_title(document, version),
        detail_url=reverse("documents:document_summary", args=(document.pk,))
        + (f"?source=search&position={result_position}" if query else ""),
        original_url=reverse("documents:document_viewer", args=(document.pk,))
        + (f"?source=search&position={result_position}" if query else ""),
        date_label=_date_label(document.archive_date, precision),
        date_value=_date_value(document.archive_date, precision),
        type_label=DocumentType(document_type).label,
        institution=(document.archive_institution or "").strip() or "医疗机构未识别",
        status_key=status_key,
        status_label=document.get_status_display(),
        observation_count=document.archive_observation_count,
        snippet=_matching_excerpt(document, version, summary, query),
        date_unknown=document.archive_date is None or precision == DatePrecision.UNKNOWN,
        result_position=result_position,
        material_label=material_state(document, version)["label"],
    )


def _group_cards(cards):
    groups = []
    for card in cards:
        if not groups or groups[-1][0] != card.date_label:
            groups.append((card.date_label, []))
        groups[-1][1].append(card)
    return tuple(RecordGroup(label=label, cards=tuple(group_cards)) for label, group_cards in groups)


def records_context(patient, parameters):
    query = parameters.get("q", "").strip()[:MAX_SEARCH_LENGTH]
    selected_type = parameters.get("type", "")
    if selected_type not in DocumentType.values:
        selected_type = ""
    selected_status = parameters.get("status", "")
    if selected_status not in DocumentStatus.values:
        selected_status = ""
    selected_year = _bounded_int(parameters.get("year"), 1900, 2100)
    selected_month = _bounded_int(parameters.get("month"), 1, 12)

    queryset = _base_queryset(patient, query)
    if selected_type:
        if selected_type == DocumentType.UNKNOWN:
            queryset = queryset.filter(Q(archive_type=selected_type) | Q(archive_type__isnull=True))
        else:
            queryset = queryset.filter(archive_type=selected_type)
    if selected_status:
        queryset = queryset.filter(status=selected_status)
    from collections import defaultdict
    by_document = defaultdict(list)
    for row in effective_rows(patient):
        by_document[row.parsing_version.document_id].append(row)
    from datetime import date
    from apps.facts.clinical_readmodels import report_material
    clinical_texts, clinical_dates = defaultdict(list), defaultdict(set)
    for report in report_material(patient):
        if not report["source_valid"] or report["status"] != "ACTIVE":
            continue
        document_id = report["document_id"]
        for field in report["fields"]:
            if not field["source_valid"] or field["status"] == "EXCLUDED":
                continue
            clinical_texts[document_id].append(f'{field["field_label"]}：{field["content"]["text"]}（{field["status_label"]}）')
            if field["usable"] and field["field_key"] == "report.exam_date" and not report["date_conflict"]:
                value = field["content"]["value"]
                if value["precision"] == "DAY":
                    clinical_dates[document_id].add(date.fromisoformat(value["value"]))
    # SQL date/value/code filters would discard a correction before resolving it.
    matching_ids = set(_apply_search(queryset, query).values_list("pk", flat=True)) if query else set()
    documents = []
    for document in queryset:
        document.archive_clinical_texts = clinical_texts[str(document.pk)]
        rows = by_document[document.pk]
        version = _active_version(document)
        unlinked = reconciliation_rows(version, rows) if version else ()
        document.archive_date, document.archive_precision = effective_document_date(rows, document.archive_date, document.archive_precision)
        document.archive_observation_count = len(rows)
        if version:
            version.archive_observations = (*rows, *unlinked)
        if query and document.pk not in matching_ids and not any(query.casefold() in source.casefold() for source in _source_values(document, version, _summary(version))):
            continue
        if selected_year or selected_month:
            dates = {document.archive_date} if document.archive_date is not None else set()
            dates.update(clinical_dates[str(document.pk)])
            # One uploaded PDF may contain several independently dated exams.
            dates.update(row.observation_date for row in rows if row.observation_date is not None
                         and not {issue["code"] for issue in validate_observation(row)} & {"date_uncertain", "date_conflict"})
            if not any((not selected_year or value.year == int(selected_year))
                       and (not selected_month or value.month == int(selected_month)) for value in dates):
                continue
        documents.append(document)
    documents.sort(key=lambda document: (document.archive_date is not None, document.archive_date.toordinal() if document.archive_date else 0, document.created_at, str(document.pk)), reverse=True)
    page = Paginator(documents, ARCHIVE_PAGE_SIZE).get_page(parameters.get("page"))
    cards = tuple(
        _card(document, query, page.start_index() + index)
        for index, document in enumerate(page.object_list)
    )
    pagination_query = urlencode(
        {
            "q": query,
            "type": selected_type,
            "status": selected_status,
            "year": selected_year,
            "month": selected_month,
        }
    )
    return {
        "current_section": "records",
        "query": query,
        "selected_type": selected_type,
        "selected_status": selected_status,
        "selected_year": selected_year,
        "selected_month": selected_month,
        "pagination_query": pagination_query,
        "type_filters": DocumentType.choices,
        "status_filters": DocumentStatus.choices,
        "record_groups": _group_cards(cards),
        "page_obj": page,
    }
