import re
from dataclasses import dataclass

from django.core.paginator import Paginator
from django.db.models import Count, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce

from apps.labs.models import LabObservation
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    OcrBlock,
    ParsingVersion,
)

from .models import Document, DocumentStatus


ARCHIVE_PAGE_SIZE = 20
MAX_SEARCH_LENGTH = 100


@dataclass(frozen=True)
class RecordCard:
    document: Document
    date_label: str
    type_label: str
    institution: str
    status_label: str
    observation_count: int
    snippet: str
    date_unknown: bool


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
    queryset = ParsingVersion.objects.filter(active=True).select_related("document_summary")
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
                    | Q(standard_name__icontains=query)
                    | Q(raw_value__icontains=query)
                    | Q(raw_unit__icontains=query)
                )
                .select_related("document_page")
                .order_by("document_page__page_number", "reading_order", "pk"),
                to_attr="archive_observations",
            ),
        )
    return queryset


def _base_queryset(patient, query):
    summaries = DocumentSummary.objects.filter(
        parsing_version__document_id=OuterRef("pk"),
        parsing_version__active=True,
    )
    observation_counts = (
        LabObservation.objects.filter(
            parsing_version__document_id=OuterRef("pk"),
            parsing_version__active=True,
        )
        .values("parsing_version__document_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return (
        Document.objects.filter(patient=patient, deleted_at__isnull=True)
        .annotate(
            archive_date=Subquery(summaries.values("document_date")[:1]),
            archive_precision=Subquery(summaries.values("date_precision")[:1]),
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
    search = (
        Q(display_filename__icontains=query)
        | Q(
            parsing_versions__active=True,
            parsing_versions__ocr_blocks__text__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__document_summary__document_date_raw__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__document_summary__institution_raw__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__lab_observations__raw_name__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__lab_observations__standard_name__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__lab_observations__raw_value__icontains=query,
        )
        | Q(
            parsing_versions__active=True,
            parsing_versions__lab_observations__raw_unit__icontains=query,
        )
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
    return queryset.filter(search).distinct()


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
            values.extend(
                (
                    observation.raw_name,
                    observation.standard_name,
                    observation.raw_value,
                    observation.raw_unit,
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


def _card(document, query):
    version = _active_version(document)
    summary = _summary(version)
    precision = document.archive_precision or DatePrecision.UNKNOWN
    document_type = document.archive_type or DocumentType.UNKNOWN
    return RecordCard(
        document=document,
        date_label=_date_label(document.archive_date, precision),
        type_label=DocumentType(document_type).label,
        institution=(document.archive_institution or "").strip(),
        status_label=document.get_status_display(),
        observation_count=document.archive_observation_count,
        snippet=_matching_excerpt(document, version, summary, query),
        date_unknown=document.archive_date is None,
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

    queryset = _apply_search(_base_queryset(patient, query), query)
    if selected_type:
        if selected_type == DocumentType.UNKNOWN:
            queryset = queryset.filter(Q(archive_type=selected_type) | Q(archive_type__isnull=True))
        else:
            queryset = queryset.filter(archive_type=selected_type)
    if selected_status:
        queryset = queryset.filter(status=selected_status)
    queryset = queryset.order_by(F("archive_date").desc(nulls_last=True), "-created_at", "-pk")

    page = Paginator(queryset, ARCHIVE_PAGE_SIZE).get_page(parameters.get("page"))
    cards = tuple(_card(document, query) for document in page.object_list)
    return {
        "current_section": "records",
        "query": query,
        "selected_type": selected_type,
        "selected_status": selected_status,
        "type_filters": DocumentType.choices,
        "status_filters": DocumentStatus.choices,
        "record_groups": _group_cards(cards),
        "page_obj": page,
    }
