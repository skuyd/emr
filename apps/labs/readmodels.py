"""Shared effective reads; raw database fields must never discard corrected rows."""

from django.urls import reverse
from django.db.models import Prefetch

from .models import LabObservation
from .quality import MIN_OBSERVATION_CONFIDENCE
from .revisions import effective_observation
from .validation import REFERENCE_BLOCKING_ISSUES, reference_comparison


def checked_reference(observation, issues, *, dictionary=None, rules=None):
    if {item["code"] for item in issues} & REFERENCE_BLOCKING_ISSUES:
        return {"label": "无法对照", "status": "unavailable"}
    return reference_comparison(observation, dictionary=dictionary, rules=rules, validated_issues=issues)


def observation_queryset():
    from apps.processing.models import DocumentMetadataCandidate, ParsingVersion
    versions = ParsingVersion.objects.select_related('document__patient__account').prefetch_related(
        Prefetch('metadata_candidates',
                 queryset=DocumentMetadataCandidate.objects.filter(kind='DOCUMENT_DATE', selected=True).select_related('evidence__document_page'),
                 to_attr='selected_date_candidates'))
    return LabObservation.objects.select_related(
        'evidence', 'report_unit',
    ).prefetch_related('document_page', 'evidence__document_page', Prefetch('parsing_version', queryset=versions)
    ).order_by("document_page__page_number", "reading_order", "pk")


def visible_observation(row):
    effective = effective_observation(row)
    if (row.evidence.confidence is None or row.evidence.confidence < MIN_OBSERVATION_CONFIDENCE) and effective.applied_revision is None:
        return None
    effective.source_url = reverse("labs:observation_source", args=(row.pk, "raw_value"))
    return effective


def effective_rows(patient, *, version=None, include_uncertain=False, include_invalid=False):
    queryset = observation_queryset().filter(
        parsing_version__document__patient=patient, parsing_version__document__deleted_at__isnull=True,
    )
    queryset = queryset.filter(parsing_version=version) if version else queryset.filter(parsing_version__active=True)
    output = []
    for row in queryset:
        # These objects were fetched for this read and are consumed immediately.
        # Standalone revision-service callers may hold stale model instances.
        row._read_snapshot = True
        item = effective_observation(row) if include_uncertain else visible_observation(row)
        if item is not None:
            item.source_url = reverse("labs:observation_source", args=(row.pk, "raw_value"))
            output.append(item)
    from .report_reads import attach_report_context
    rows = attach_report_context(output)
    return rows if include_invalid else tuple(row for row in rows if row.report_identity.status != 'REJECTED')


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
    identities = [row.report_identity for row in rows if getattr(row, 'report_identity', None)
                  and row.report_identity.status != 'REJECTED']
    if identities:
        dates = {identity.sampled_at.date() if identity.status == 'ACCEPTED' and identity.sampled_at else None
                 for identity in identities}
        return (next(iter(dates)), 'DAY') if len(dates) == 1 and None not in dates else (None, 'UNKNOWN')
    corrected = {row.observation_date for row in rows if getattr(row, "date_verified", False)}
    if corrected:
        return (next(iter(corrected)), "DAY") if len(corrected) == 1 and None not in corrected else (None, "UNKNOWN")
    return fallback, precision
