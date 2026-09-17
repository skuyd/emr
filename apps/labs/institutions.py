"""Batch-resolve institutions only from the active report's own evidence."""

from collections import defaultdict

from apps.processing.models import DocumentMetadataCandidate, DocumentSummary, MetadataKind
from .quality import MIN_STANDARD_NAME_CONFIDENCE


def comparison_institutions(rows):
    versions = {row.parsing_version_id for row in rows}
    candidates = defaultdict(list)
    for candidate in DocumentMetadataCandidate.objects.filter(parsing_version_id__in=versions, kind=MetadataKind.INSTITUTION).select_related('evidence'):
        if (candidate.confidence >= MIN_STANDARD_NAME_CONFIDENCE and candidate.evidence
                and candidate.evidence.confidence is not None and candidate.evidence.confidence >= MIN_STANDARD_NAME_CONFIDENCE):
            candidates[candidate.parsing_version_id].append(candidate)
    summaries = {summary.parsing_version_id: summary for summary in DocumentSummary.objects.filter(parsing_version_id__in=versions)}
    result = {}
    for row in rows:
        records = candidates[row.parsing_version_id]
        names = {item.normalized_value.strip() for item in records if item.normalized_value.strip()}
        page_names = {item.normalized_value.strip() for item in records if item.normalized_value.strip()
                      and item.evidence.document_page_id == row.document_page_id}
        raw = row.institution_raw.strip()
        evidence = row.field_evidence.get('institution_raw', {})
        conflicts = any(item.get('code') in {'association_conflict', 'recognition_uncertain', 'revision_conflict'}
                        and (not item.get('fields') or 'institution_raw' in item['fields']) for item in row.quality_issues)
        if (raw and evidence.get('page_number') == row.document_page.page_number and not conflicts
                and row.evidence.confidence is not None and row.evidence.confidence >= MIN_STANDARD_NAME_CONFIDENCE):
            name = raw
        elif len(names) > 1:
            name = next(iter(page_names)) if len(page_names) == 1 and not conflicts else '多机构，待核对'
        elif len(names) == 1 and not conflicts:
            name = next(iter(names))
        else:
            summary = summaries.get(row.parsing_version_id)
            name = summary.institution_raw.strip() if summary and summary.confidence >= MIN_STANDARD_NAME_CONFIDENCE and not conflicts else ''
        result[str(row.pk)] = name or '医院未识别'
    return result
