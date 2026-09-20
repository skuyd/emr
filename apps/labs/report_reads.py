"""Read original sampling evidence without re-running historical OCR."""

from collections import defaultdict
from copy import copy
from dataclasses import replace

from apps.processing.models import DocumentMetadataCandidate, OcrBlock
from apps.processing.value_objects import OcrPage, OcrRegion
from .report_identity import ReportUnitEvidence, extract_report_units, match_report_unit, resolve_continuation_times
from .reports import current_report_units, effective_report, ensure_historical_report_units, relation_has_conflict, report_relations


def _historical_units(rows):
    missing = [row for row in rows if row.report_unit_id is None]
    versions = {row.parsing_version_id for row in missing}
    pages = {row.document_page_id: row.document_page for row in missing}
    blocks = defaultdict(list)
    for block in OcrBlock.objects.filter(parsing_version_id__in=versions, document_page_id__in=pages).order_by('reading_order'):
        blocks[(block.parsing_version_id, block.document_page_id)].append(block)
    candidates = defaultdict(list)
    institution_candidates = defaultdict(list)
    for candidate in DocumentMetadataCandidate.objects.filter(parsing_version_id__in=versions,
            evidence__document_page_id__in=pages).select_related('evidence'):
        if candidate.evidence.source_text.strip():
            evidence = copy(candidate.evidence)
            evidence.confidence = min(candidate.confidence, evidence.confidence or 0)
            candidates[(candidate.parsing_version_id, evidence.document_page_id)].append(evidence)
            if candidate.kind == 'INSTITUTION' and candidate.confidence >= .9 and (candidate.evidence.confidence or 0) >= .9:
                institution_candidates[(candidate.parsing_version_id, candidate.evidence.document_page_id)].append(candidate)
    result = {}
    for row in missing:
        key = row.parsing_version_id, row.document_page_id
        if key in result:
            continue
        items = blocks[key] or candidates[key]
        if not items:
            result[key] = ()
            continue
        all_located = all(item.polygon for item in items)
        regions = tuple(OcrRegion(getattr(item, 'text', None) or item.source_text,
            tuple(tuple(point) for point in item.polygon) if item.polygon else ((0., 0.), (1., 0.), (1., 1.), (0., 1.)),
            float(item.confidence or 0), index) for index, item in enumerate(items))
        page = OcrPage(row.document_page.page_number, row.document_page.width, row.document_page.height,
            regions, 'historical-evidence', '1', source_transform=((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)) if all_located else None)
        result[key] = extract_report_units((page,), lab_page_numbers=(page.page_number,))
        # Legacy metadata explicitly typed the institution and kept its own
        # page evidence. A display summary or another page is not evidence.
        if not blocks[key] and len(result[key]) == 1 and institution_candidates[key]:
            identity = result[key][0]
            names = {item.normalized_value.strip() for item in institution_candidates[key] if item.normalized_value.strip()}
            fields = dict(identity.fields)
            fields['institution'] = [{'value': item.normalized_value.strip(), 'raw_text': item.evidence.source_text,
                'page_number': page.page_number, 'polygon': item.evidence.polygon, 'confidence': float(item.evidence.confidence)}
                for item in institution_candidates[key]]
            identity = replace(identity, institution=next(iter(names)) if len(names) == 1 else '', fields=fields)
            if len(names) > 1 and identity.status == 'ACCEPTED':
                identity = replace(identity, status='REVIEW', reason='report_identity_conflict', identity_reliable=False)
            result[key] = (identity,)
    return result


def read_report_identities(patient, units, *, allowed_source_keys=None):
    """Project borrowed clocks from current, authorized evidence, keeping the audit immutable."""
    units = tuple(units)
    identities = {unit.pk: effective_report(unit) for unit in units}
    borrowed = {identity.time_source for identity in identities.values() if identity.time_source}
    if not borrowed:
        return identities
    if allowed_source_keys is not None:
        borrowed &= set(allowed_source_keys)
    available = {unit.source_key: unit for unit in current_report_units(patient).filter(source_key__in=borrowed)}
    donors = {key: effective_report(unit) for key, unit in available.items()}
    relations = {frozenset((item.left_key, item.right_key)): item for item in report_relations(patient)} if donors else {}
    for unit in units:
        identity = identities[unit.pk]
        if not identity.time_source:
            continue
        donor = donors.get(identity.time_source)
        relation = relations.get(frozenset((unit.source_key, identity.time_source)))
        unavailable = donor is None or (relation and relation.state in {'UNDONE', 'DIFFERENT'})
        projected = replace(identity, sampled_at=None, precision='', status='REJECTED' if unavailable else 'REVIEW',
            reason='sampling_source_unavailable' if unavailable else 'sampling_source_changed')
        if allowed_source_keys is not None and identity.time_source not in allowed_source_keys:
            projected = replace(projected, time_source='')
        if (not unavailable and relation and relation.state in {'AUTO', 'SAME'}
                and not relation.basis.get('overlap_conflict')
                and not relation.basis.get('identity_or_result_uncertain') and not donor.time_source):
            # Re-use the admission rules, including own dates/clock-only text,
            # page sequence and patient identity. A copied old clock is not evidence.
            candidate = replace(projected, status='REJECTED', reason='sampling_datetime_missing')
            resolved = resolve_continuation_times(((identity.time_source, donor), (unit.source_key, candidate)))
            if resolved[unit.source_key].status == 'ACCEPTED':
                projected = replace(resolved[unit.source_key], fields={**identity.fields,
                    'sampling_time_source': donor.fields.get('sampled_at', ())})
        identities[unit.pk] = projected
    return identities


def attach_report_context(rows, *, allowed_source_keys=None):
    rows = tuple(rows)
    missing = [row for row in rows if row.report_unit_id is None and row.parsing_version.active]
    if missing:
        from .models import LabObservation

        patients = {row.parsing_version.document.patient_id: row.parsing_version.document.patient for row in missing}
        for patient in patients.values():
            ensure_historical_report_units(patient)
        assigned = {row.pk: row.report_unit for row in LabObservation.objects.filter(
            pk__in=[row.pk for row in missing]).select_related('report_unit')}
        for row in missing:
            row.report_unit = assigned[row.pk]
    historical = _historical_units(rows)
    by_patient = defaultdict(dict)
    patients = {}
    for row in rows:
        if row.report_unit_id:
            patient = row.parsing_version.document.patient
            patients[patient.pk] = patient
            by_patient[patient.pk][row.report_unit_id] = row.report_unit
    units = {key: identity for patient_id, selected in by_patient.items()
             for key, identity in read_report_identities(patients[patient_id], selected.values(),
                 allowed_source_keys=allowed_source_keys).items()}
    conflicts = set()
    for patient_id, selected in by_patient.items():
        keys = ({unit.source_key for unit in selected.values()} & set(allowed_source_keys)
                if allowed_source_keys is not None else None)
        for relation in report_relations(patients[patient_id]):
            if (keys is None or relation.left_key in keys and relation.right_key in keys) and relation_has_conflict(relation):
                conflicts.update((relation.left_key, relation.right_key))
    for row in rows:
        row.report_conflict = bool(row.parsing_version.active and row.report_unit_id and row.report_unit.source_key in conflicts)
        if row.report_unit_id:
            identity = units[row.report_unit_id]
        else:
            candidates = historical.get((row.parsing_version_id, row.document_page_id), ())
            index = match_report_unit(row, candidates)
            identity = candidates[index] if index is not None else ReportUnitEvidence(row.document_page.page_number, 0, 0)
        if not hasattr(row, 'date_before_report_projection'):
            row.date_before_report_projection = row.observation_date
        if getattr(row, 'date_verified', False) and (identity.sampled_at is None or row.date_before_report_projection != identity.sampled_at.date()):
            # A date-only observation edit cannot supply a report's clock or
            # silently override a contradictory report-level transcription.
            identity = replace(identity, status='REVIEW' if identity.sampled_at else 'REJECTED',
                               reason='sampling_datetime_conflict' if identity.sampled_at else 'sampling_datetime_missing')
        row.report_identity = identity
        names = {item['value'] for item in identity.fields.get('institution', ())}
        row.comparison_institution = identity.institution or ('多机构，待核对' if len(names) > 1 else '医院未识别')
        row.observation_date = identity.sampled_at.date() if identity.sampled_at else (
            identity.sampling_dates[0] if len(identity.sampling_dates) == 1 and identity.status == 'REVIEW' else None)
    return rows


def assign_report_groups(patient, rows):
    """Count report relations only among sources in the caller's visible scope."""
    source_keys = {row.report_unit.source_key for row in rows if row.report_unit_id}
    groups = {key: {key} for key in source_keys}
    relations = [item for item in report_relations(patient)
                 if item.left_key in source_keys and item.right_key in source_keys]
    joined = {frozenset((item.left_key, item.right_key)) for item in relations if item.state in {'AUTO', 'SAME'}}
    for relation in relations:
        left, right = groups[relation.left_key], groups[relation.right_key]
        if left is right or not all(frozenset((a, b)) in joined for a in left for b in right):
            continue
        combined = left | right
        for key in combined:
            groups[key] = combined
    for row in rows:
        row.report_group_key = (min(groups[row.report_unit.source_key]) if row.report_unit_id else
                                f'{row.document_page_id}:{row.report_identity.start_order}')
