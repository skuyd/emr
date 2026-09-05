"""Shared effective reads; raw database fields must never discard corrected rows."""

from django.urls import reverse

from .models import LabObservation
from .quality import MIN_OBSERVATION_CONFIDENCE
from .revisions import effective_observation
from .validation import REFERENCE_BLOCKING_ISSUES, reference_comparison


def checked_reference(observation, issues, *, dictionary=None, rules=None):
    if {item["code"] for item in issues} & REFERENCE_BLOCKING_ISSUES:
        return {"label": "无法对照", "status": "unavailable"}
    return reference_comparison(observation, dictionary=dictionary, rules=rules)


def observation_queryset():
    return LabObservation.objects.select_related(
        "parsing_version__document__patient__account", "document_page", "evidence__document_page",
    ).order_by("document_page__page_number", "reading_order", "pk")


def visible_observation(row):
    effective = effective_observation(row)
    if (row.evidence.confidence is None or row.evidence.confidence < MIN_OBSERVATION_CONFIDENCE) and effective.applied_revision is None:
        return None
    effective.source_url = reverse("labs:observation_source", args=(row.pk, "raw_value"))
    return effective


def effective_rows(patient, *, version=None, include_uncertain=False):
    queryset = observation_queryset().filter(
        parsing_version__document__patient=patient, parsing_version__document__deleted_at__isnull=True,
    )
    queryset = queryset.filter(parsing_version=version) if version else queryset.filter(parsing_version__active=True)
    output = []
    for row in queryset:
        item = effective_observation(row) if include_uncertain else visible_observation(row)
        if item is not None:
            item.source_url = reverse("labs:observation_source", args=(row.pk, "raw_value"))
            output.append(item)
    return tuple(output)


def reconciliation_rows(version, current):
    """Retain edits that cannot be matched uniquely to the selected parse lineage."""
    represented = {str(row.pk) for row in current}
    represented.update(source["observation_id"] for row in current for source in row.value_sources.values())
    versions, visited = [], set()
    previous = version.previous_version
    while previous is not None and previous.pk not in visited:
        visited.add(previous.pk)
        versions.append(previous.pk)
        previous = previous.previous_version
    output = []
    for row in observation_queryset().filter(parsing_version_id__in=versions, revision_number__gt=0):
        if str(row.pk) in represented:
            continue
        effective = effective_observation(row)
        effective.revision_conflict = True
        effective.reconciliation_label = "此前人工修订未能与本次识别唯一关联，请切换原版本核对"
        effective.source_url = reverse("labs:observation_source", args=(row.pk, "raw_value"))
        output.append(effective)
    return tuple(output)


def effective_document_date(rows, fallback, precision):
    corrected = {row.observation_date for row in rows if getattr(row, "date_verified", False)}
    if corrected:
        return (next(iter(corrected)), "DAY") if len(corrected) == 1 and None not in corrected else (None, "UNKNOWN")
    return fallback, precision
