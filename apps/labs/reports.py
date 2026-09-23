"""Report evidence and revocable associations, separate from result folding."""

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from itertools import combinations

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.patients.access import authorize_patient
from apps.patients.models import Patient
from apps.processing.value_objects import normalized_polygon
from .extraction import _unit_key
from .models import LabReportUnit, LabReportRevision, ReportAssociation, ReportAssociationEvent
from .report_identity import ReportUnitEvidence, compatible_times, match_report_unit, _same_patient_fields, contains_source_location
from .revisions import effective_observation


class ReportDecisionConflict(ValueError):
    pass


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _snapshot(identity):
    return _plain(asdict(identity))


def _from_snapshot(values):
    values = dict(values)
    values['sampled_at'] = datetime.fromisoformat(values['sampled_at']) if values.get('sampled_at') else None
    values['sampling_dates'] = tuple(date.fromisoformat(day) for day in values.get('sampling_dates', ()))
    return ReportUnitEvidence(**values)


@dataclass(frozen=True)
class ReportRevisionState:
    identity: ReportUnitEvidence
    candidate: ReportUnitEvidence
    revision: LabReportRevision | None = None
    inherited: bool = False


def _inherited_report_revision(unit):
    from apps.processing.models import ParsingVersion

    version_id = unit.parsing_version.previous_version_id
    region = unit.automatic.get('source_region')
    current = list(LabReportUnit.objects.filter(parsing_version=unit.parsing_version,
                                               document_page=unit.document_page))
    if sum(item.automatic.get('source_region') == region for item in current) != 1:
        return None
    visited = {unit.parsing_version_id}
    while version_id and version_id not in visited:
        visited.add(version_id)
        version = ParsingVersion.objects.filter(pk=version_id, document_id=unit.parsing_version.document_id).first()
        if version is None:
            break
        candidates = list(version.lab_report_units.filter(document_page=unit.document_page))
        exact = [item for item in candidates if item.automatic.get('source_region') == region]
        # Ordinals are presentation order, not proof that a report survived a split.
        if len(exact) != 1 or (region is None and (len(candidates) != 1 or len(current) != 1)):
            break
        revision = exact[0].revisions.select_related('unit').order_by('-sequence').first()
        if revision:
            return revision
        version_id = version.previous_version_id
    return None


def _carry_report_fields(automatic, revised):
    result = automatic
    for name in ('sampled_at', 'institution', 'report_number'):
        fields = revised.fields.get(name, ())
        if len(fields) == 1 and fields[0].get('origin') == 'USER':
            source = {key: value for key, value in fields[0].items() if key not in {'value', 'raw_text', 'origin'}}
            value = revised.sampling_label if name == 'sampled_at' else getattr(revised, name)
            result = _corrected(result, {name: value}, source)
    return result


def report_revision_state(unit):
    automatic = _from_snapshot(unit.automatic)
    revision = unit.revisions.select_related('unit').order_by('-sequence').first() if unit.revision_number else None
    inherited = False
    if revision is None and unit.parsing_version.previous_version_id:
        revision = _inherited_report_revision(unit)
        inherited = revision is not None
    if revision is None:
        return ReportRevisionState(automatic, automatic)
    revised = _from_snapshot(revision.after)
    candidate = _carry_report_fields(automatic, revised)
    if inherited:
        identity = candidate
        if unit.automatic != revision.unit.automatic or revised.reason == 'report_revision_conflict':
            identity = replace(identity, status='REVIEW', reason='report_revision_conflict')
    else:
        identity = revised
    return ReportRevisionState(identity, candidate, revision, inherited)


def effective_report(unit):
    return report_revision_state(unit).identity


def report_revision_history(unit):
    return LabReportRevision.objects.filter(unit__parsing_version__document_id=unit.parsing_version.document_id,
        unit__document_page=unit.document_page, unit__parsing_version__status='PUBLISHED'
    ).select_related('author', 'unit__parsing_version', 'unit__document_page').order_by('-created_at', '-pk')


def report_source_token(unit, state=None):
    state = state or report_revision_state(unit)
    basis = {'unit': str(unit.pk), 'revision': unit.revision_number,
             'published_at': str(unit.parsing_version.published_at),
             'applied_revision': state.revision.pk if state.revision else None,
             'automatic': unit.automatic, 'effective': _snapshot(state.identity)}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@transaction.atomic
def persist_report_units(version, identities):
    """Only call after admission; retries cannot rewrite original OCR evidence."""
    from apps.documents.models import DocumentPage

    pages = {page.page_number: page for page in DocumentPage.objects.filter(document_id=version.document_id)}
    counts, units = defaultdict(int), []
    for identity in identities:
        page = pages[identity.page_number]
        counts[page.pk] += 1
        ordinal = identity.ordinal or counts[page.pk]
        automatic = _snapshot(identity)
        unit, created = LabReportUnit.objects.get_or_create(parsing_version=version, document_page=page,
            ordinal=ordinal, defaults={'source_key': f'{version.document_id}:{page.page_number}:{ordinal}', 'automatic': automatic})
        comparison = automatic
        if not created and 'physiological_phase' not in unit.automatic.get('fields', {}):
            comparison = {**automatic, 'fields': {key: value for key, value in automatic['fields'].items()
                                                 if key != 'physiological_phase'}}
        if not created and unit.automatic != comparison:
            raise ReportDecisionConflict('已保存的报告原始证据不能被重试覆盖。')
        units.append(unit)
    for page_id, count in counts.items():
        page_units = [unit for unit in units if unit.document_page_id == page_id]
        rows = version.lab_observations.filter(document_page_id=page_id)
        if count == 1:
            rows.update(report_unit=page_units[0])
        else:
            # Multiple reports require a location match, never just row order.
            for row in rows.select_related('evidence'):
                index = match_report_unit(row, tuple(_from_snapshot(unit.automatic) for unit in page_units))
                row.report_unit = page_units[index] if index is not None else None
                row.save(update_fields=['report_unit'])
    from .phases import report_phase
    phases = {unit.pk: report_phase(_from_snapshot(unit.automatic)) for unit in units}
    for row in version.lab_observations.filter(report_unit_id__in=phases, physiological_phase='', phase_raw=''):
        phase, raw, evidence = phases[row.report_unit_id]
        if not raw:
            continue
        row.physiological_phase, row.phase_raw = phase, raw
        proof = evidence[0]
        row.field_evidence = {**row.field_evidence, 'physiological_phase': {
            'page_number': proof['page_number'], 'polygon': proof['polygon'],
            'text': raw, 'reading_order': proof.get('reading_order'),
        }}
        row.save(update_fields=['physiological_phase', 'phase_raw', 'field_evidence'])
    return tuple(units)


def result_identity(row):
    """Exact values only: no rounding, conversion or qualitative synonym map."""
    value = row.raw_value.strip()
    if row.result_type in {'NUMERIC', 'COMPARATOR'}:
        match = re.fullmatch(r'([<>≤≥]=?)?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)', value)
        if not match or (row.result_type == 'COMPARATOR') != bool(match.group(1)):
            return None
        try:
            number = Decimal(match.group(2))
        except InvalidOperation:
            return None
        if not number.is_finite():
            return None
        # Keep equality stable when serialized for a frozen selection. Decimal
        # strings retain trailing zeros; normalize() can round using the context.
        sign, digits, exponent = (number if number else Decimal(0)).as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        value = (match.group(1) or '', (sign, tuple(digits), exponent))
    return row.result_type, value, _unit_key(row.raw_unit)


def _rows(unit):
    from .validation import indicator_identity_issue

    output = []
    for row in unit.observations.select_related('parsing_version__document', 'document_page', 'evidence').order_by('pk'):
        row._read_snapshot = True
        current = effective_observation(row)
        current.indicator_identity_issue = indicator_identity_issue(current)
        output.append(current)
    return tuple(output)


def _overlap_conflict(left_rows, right_rows):
    def key(row):
        return row.standard_code, row.specimen

    values = defaultdict(set)
    for row in left_rows:
        values[key(row)].add(result_identity(row))
    return any(key(row) in values and (len(values[key(row)]) != 1 or result_identity(row) not in values[key(row)])
               for row in right_rows)


def _uncertain(rows):
    from .validation import indicator_identity_issue

    fields = {'raw_name', 'standard_code', 'raw_value', 'raw_unit', 'result_type', 'specimen'}
    return any(row.standard_code.startswith('CANDIDATE_') or not row.raw_unit.strip()
               or (row.indicator_identity_issue if hasattr(row, 'indicator_identity_issue') else indicator_identity_issue(row)) is not None
               or getattr(row, 'reported_error', False) or getattr(row, 'revision_conflict', False)
               or result_identity(row) is None
               or any(item.get('code') in {'mapping_unknown', 'recognition_uncertain', 'association_conflict',
                                          'normalization_uncertain', 'type_conflict', 'specimen_conflict'}
                      and (not item.get('fields') or fields.intersection(item['fields'])) for item in row.quality_issues)
               for row in rows)


def _pair_basis(left, right, *, sources=None):
    first, left_rows = sources[left.source_key] if sources is not None else (effective_report(left), _rows(left))
    second, right_rows = sources[right.source_key] if sources is not None else (effective_report(right), _rows(right))
    rows = left_rows + right_rows
    time_matches = compatible_times(first.sampled_at, first.precision, second.sampled_at, second.precision)
    overlap_conflict = _overlap_conflict(left_rows, right_rows) or _overlap_conflict(right_rows, left_rows)
    reliable = (first.status == second.status == 'ACCEPTED' and first.identity_reliable and second.identity_reliable
                and first.report_number == second.report_number and first.institution == second.institution
                and time_matches and _same_patient_fields(first, second) and not overlap_conflict and not _uncertain(rows))
    basis = {'left': _snapshot(first), 'right': _snapshot(second), 'time_compatible': time_matches,
             'overlap_conflict': overlap_conflict, 'identity_or_result_uncertain': _uncertain(rows),
             'sources': [{'id': str(row.pk), 'source_key': unit.source_key,
                          'revision': row.revision_number, 'name': row.standard_code,
                          'value': row.raw_value, 'unit': row.raw_unit, 'type': row.result_type,
                          'specimen': row.specimen, 'issues': row.quality_issues,
                          'evidence': {'text': row.evidence.source_text, 'polygon': row.evidence.polygon,
                                       'confidence': str(row.evidence.confidence)},
                          'field_evidence': row.field_evidence}
                         for unit, items in ((left, left_rows), (right, right_rows)) for row in items]}
    fingerprint = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return first, second, reliable, basis, fingerprint


def _same_source_evidence(left, right):
    def without_row_ids(basis):
        return {**basis, 'sources': sorted(
            ({key: value for key, value in row.items() if key != 'id'} for row in basis.get('sources', ())),
            key=lambda row: json.dumps(row, sort_keys=True))}
    return without_row_ids(left) == without_row_ids(right)


def relation_has_conflict(relation):
    """Insufficient identity alone is not a contradictory report claim."""
    if relation.state == 'DIFFERENT':
        return False
    left = _from_snapshot(relation.basis['left'])
    right = _from_snapshot(relation.basis['right'])
    same_identity = (bool(left.report_number and left.institution)
                     and left.report_number == right.report_number and left.institution == right.institution)
    if not same_identity and relation.state != 'SAME':
        return False
    borrowed = left.time_source == relation.right_key or right.time_source == relation.left_key
    # A continuation's stored clock is an immutable admission snapshot. Its
    # current clock (or rejection) is resolved from the live donor by reads.
    return (bool(not borrowed and left.sampled_at and right.sampled_at and not relation.basis['time_compatible'])
            or not _same_patient_fields(left, right) or relation.basis['overlap_conflict'])


def _record(association, *, state, action, basis, fingerprint, actor=None, rationale='', operation_id):
    before = association.state
    association.state = state
    association.revision_number += 1
    association.basis = basis
    association.evidence_fingerprint = fingerprint
    association.save()
    ReportAssociationEvent.objects.create(association=association, author_id=getattr(actor, 'pk', actor),
        sequence=association.revision_number, action=action, before_state=before, after_state=state,
        basis=basis, evidence_fingerprint=fingerprint, rationale=rationale, operation_id=operation_id)


def current_report_units(patient):
    return LabReportUnit.objects.filter(parsing_version__active=True,
        parsing_version__document__patient=patient, parsing_version__document__deleted_at__isnull=True,
    ).select_related('parsing_version__document', 'document_page').order_by('source_key')


def resolve_admitted_continuations(document, identities, pages, dictionary):
    """An admission cache is not permission to reuse an obsolete donor."""
    from apps.documents.intake import decode_pages
    from apps.documents.models import UploadIntake
    from apps.processing.errors import RetryableProcessingError
    from apps.processing.models import ParsingVersion
    from .extraction import extract_observations
    from .report_identity import resolve_continuation_times

    borrowed = {identity.time_source for identity in identities if identity.time_source}
    if not borrowed:
        return identities
    donors = {unit.source_key: (effective_report(unit), _rows(unit)) for unit in
              current_report_units(document.patient).filter(source_key__in=borrowed,
                  parsing_version__document__batch_id=document.batch_id)}
    pending = UploadIntake.objects.filter(item__batch_id=document.batch_id,
        item_id__in={key.split(':')[0] for key in borrowed}).select_related('item__document')
    for intake in pending:
        source_document = intake.item.document
        if source_document is None:
            if intake.state == 'READY' and intake.recognition.get('admitted'):
                raise RetryableProcessingError('sampling_source_pending')
            continue
        if source_document.deleted_at or ParsingVersion.objects.filter(document=source_document, active=True).exists():
            continue
        for values in intake.recognition.get('units', ()):
            source = _from_snapshot(values)
            key = f'{source_document.pk}:{source.page_number}:{source.ordinal}'
            if key not in borrowed:
                continue
            selected = tuple(replace(page, regions=tuple(region for region in page.regions
                if source.start_order <= region.reading_order <= source.end_order))
                for page in decode_pages(intake.recognition['pages']) if page.page_number == source.page_number)
            donors[key] = source, extract_observations(selected, dictionary)
    output = []
    for identity in identities:
        if not identity.time_source:
            output.append(identity)
            continue
        candidate = replace(identity, sampled_at=None, precision='', status='REJECTED',
                            reason='sampling_source_unavailable')
        donor = donors.get(identity.time_source)
        if donor and not donor[0].time_source:
            source, right_rows = donor
            selected = tuple(replace(page, regions=tuple(region for region in page.regions
                if identity.start_order <= region.reading_order <= identity.end_order))
                for page in pages if page.page_number == identity.page_number)
            left_rows = extract_observations(selected, dictionary)
            if not (_overlap_conflict(left_rows, right_rows) or _overlap_conflict(right_rows, left_rows)
                    or _uncertain(tuple(left_rows) + tuple(right_rows))):
                key = f'{document.pk}:{identity.page_number}:{identity.ordinal}'
                candidate = resolve_continuation_times(((identity.time_source, source), (key, candidate)))[key]
        output.append(candidate)
    return tuple(output)


def resolve_reprocessed_continuations(document, identities, pages, dictionary):
    """Recheck an existing time link against new OCR and the current main report."""
    from .extraction import extract_observations
    from .report_identity import resolve_continuation_times
    from .report_reads import read_report_identities

    previous = tuple(current_report_units(document.patient).filter(parsing_version__document=document))
    if not previous:
        return identities
    current = read_report_identities(document.patient, previous)
    prior = {unit.source_key: current[unit.pk] for unit in previous}
    borrowed = {identity.time_source for identity in prior.values()
                if identity.status == 'ACCEPTED' and identity.time_source}
    donors = {unit.source_key: unit for unit in current_report_units(document.patient).filter(
        source_key__in=borrowed, parsing_version__document__batch_id=document.batch_id)}
    fresh = {f'{document.pk}:{identity.page_number}:{identity.ordinal}': identity for identity in identities}

    def new_rows(identity):
        selected = tuple(replace(page, regions=tuple(region for region in page.regions
                         if identity.start_order <= region.reading_order <= identity.end_order))
                         for page in pages if page.page_number == identity.page_number)
        return extract_observations(selected, dictionary)

    output = []
    for key, identity in fresh.items():
        old = prior.get(key)
        donor = donors.get(old.time_source) if old and old.status == 'ACCEPTED' else None
        if identity.status == 'REJECTED' and donor:
            source = fresh.get(donor.source_key) or effective_report(donor)
            left_rows = new_rows(identity)
            right_rows = new_rows(source) if donor.source_key in fresh else _rows(donor)
            if not (_overlap_conflict(left_rows, right_rows) or _overlap_conflict(right_rows, left_rows)
                    or _uncertain(left_rows + right_rows)):
                resolved = resolve_continuation_times(((donor.source_key, source), (key, identity)))
                identity = resolved[key]
        output.append(identity)
    return tuple(output)


@transaction.atomic
def ensure_historical_report_units(patient):
    """Materialize source evidence only; never infer clocks or re-run OCR."""
    from .readmodels import observation_queryset
    from .report_reads import _historical_units

    Patient.objects.select_for_update(no_key=True).get(pk=patient.pk)
    existing = set(current_report_units(patient).values_list('parsing_version_id', 'document_page_id'))
    rows = tuple(row for row in observation_queryset().filter(report_unit__isnull=True,
        parsing_version__active=True, parsing_version__document__patient=patient,
        parsing_version__document__deleted_at__isnull=True)
        if (row.parsing_version_id, row.document_page_id) not in existing)
    identities = _historical_units(rows)
    grouped, versions = defaultdict(list), {}
    for row in rows:
        key = row.parsing_version_id, row.document_page_id
        if key not in identities:
            continue
        units = identities.pop(key)
        if not units:
            units = (ReportUnitEvidence(row.document_page.page_number, 0, 0, ordinal=1,
                fields={name: [] for name in ('sampled_at', 'report_number', 'institution', 'patient', 'page')}),)
        grouped[row.parsing_version_id].extend(units)
        versions[row.parsing_version_id] = row.parsing_version
    for version_id, units in grouped.items():
        persist_report_units(versions[version_id], units)


@transaction.atomic
def propose_relation(patient, actor, left_id, right_id):
    authorize_patient(patient, actor, 'write', lock=True)
    units = tuple(current_report_units(patient).filter(pk__in=(left_id, right_id)))
    if len(units) != 2:
        raise ValueError('请选择本患者的两份不同报告。')
    left, right = units
    _, _, _, basis, fingerprint = _pair_basis(left, right)
    association, created = ReportAssociation.objects.get_or_create(patient=patient,
        left_key=left.source_key, right_key=right.source_key,
        defaults={'state': 'REVIEW', 'evidence_fingerprint': fingerprint})
    if created:
        _record(association, state='REVIEW', action='PROPOSE', basis=basis, fingerprint=fingerprint,
                actor=actor, operation_id='propose:' + fingerprint)
    return association


@transaction.atomic
def report_relations(patient):
    # Same patient guard as edits, deletion, upload and access revocation.
    Patient.objects.select_for_update(no_key=True).get(pk=patient.pk)
    units = {unit.source_key: unit for unit in current_report_units(patient)}
    identities = {key: effective_report(unit) for key, unit in units.items()}
    existing = {(item.left_key, item.right_key): item for item in ReportAssociation.objects.filter(patient=patient)}
    pairs = {pair for pair in existing if pair[0] in units and pair[1] in units}
    by_number = defaultdict(list)
    for key, identity in identities.items():
        if identity.report_number:
            by_number[identity.report_number].append(key)
    for keys in by_number.values():
        pairs.update(combinations(sorted(keys), 2))
    involved = {key for pair in pairs for key in pair}
    sources = {key: (identities[key], _rows(units[key])) for key in involved}
    output = []
    for left_key, right_key in sorted(pairs):
        left, right = units[left_key], units[right_key]
        association = existing.get((left_key, right_key))
        _, _, reliable, basis, fingerprint = _pair_basis(left, right, sources=sources)
        if association is None:
            association = ReportAssociation.objects.create(patient=patient, left_key=left_key,
                right_key=right_key, state='REVIEW', evidence_fingerprint=fingerprint)
            state = 'AUTO' if reliable else 'REVIEW'
            _record(association, state=state, action=state, basis=basis, fingerprint=fingerprint,
                    operation_id='initial:' + fingerprint)
        elif association.evidence_fingerprint != fingerprint:
            # A new parsing version changes row IDs, but cannot by itself undo
            # a valid or explicitly rejected association. Keep the source refresh audited.
            unchanged = _same_source_evidence(association.basis, basis)
            _record(association, state=association.state if unchanged else 'REVIEW',
                    action='SOURCE_REFRESH' if unchanged else 'EVIDENCE_CHANGED', basis=basis, fingerprint=fingerprint,
                    operation_id=f'evidence:{association.revision_number + 1}:{fingerprint}')
        output.append(association)
    return tuple(output)


@transaction.atomic
def decide_relation(patient, actor, association_id, action, *, expected_revision, rationale, operation_id):
    authorize_patient(patient, actor, 'write', lock=True)
    association = ReportAssociation.objects.select_for_update().filter(pk=association_id, patient=patient).first()
    if association is None:
        raise PermissionDenied
    if action not in {'SAME', 'DIFFERENT', 'UNDO'} or not rationale.strip() or not operation_id or len(operation_id) > 96:
        raise ValueError('请选择核对结论并填写原件依据。')
    prior = association.events.filter(operation_id=operation_id).first()
    if prior:
        if prior.action != action or prior.rationale != rationale or prior.author_id != getattr(actor, 'pk', actor):
            raise ReportDecisionConflict('重复请求与原决定不一致。')
        return association
    if association.revision_number != expected_revision:
        raise ReportDecisionConflict('报告关系已变化，请刷新后核对。')
    units = {item.source_key: item for item in current_report_units(patient).filter(source_key__in=[association.left_key, association.right_key])}
    if len(units) != 2:
        raise ReportDecisionConflict('原件已删除或解析版本已变化。')
    _, _, _, basis, fingerprint = _pair_basis(units[association.left_key], units[association.right_key])
    if fingerprint != association.evidence_fingerprint:
        raise ReportDecisionConflict('原件依据已变化，请刷新后核对。')
    if action == 'UNDO' and association.state not in {'AUTO', 'SAME'}:
        raise ReportDecisionConflict('当前没有可撤销的归并。')
    _record(association, state='UNDONE' if action == 'UNDO' else action, action=action, basis=basis,
            fingerprint=fingerprint, actor=actor, rationale=rationale, operation_id=operation_id)
    return association


@transaction.atomic
def correct_report(patient, actor, unit_id, changes, *, expected_revision, source_evidence, rationale, operation_id,
                   expected_source=None, action='CORRECT'):
    authorize_patient(patient, actor, 'write', lock=True)
    unit = current_report_units(patient).select_for_update().filter(pk=unit_id).first()
    if unit is None:
        raise PermissionDenied
    if (action not in {'CORRECT', 'KEEP_REVISION', 'USE_AUTOMATIC'}
            or (action == 'CORRECT' and (not changes or set(changes) - {'sampled_at', 'institution', 'report_number'}))
            or (action != 'CORRECT' and changes) or not rationale.strip() or not operation_id or len(operation_id) > 96):
        raise ValueError('请选择报告字段并填写原件依据。')
    prior = unit.revisions.filter(operation_id=operation_id).first()
    if prior:
        after = _corrected(_from_snapshot(prior.before), changes, source_evidence) if action == 'CORRECT' else _from_snapshot(prior.after)
        if (prior.action != action or _snapshot(after) != prior.after or prior.source_evidence != source_evidence
                or prior.rationale != rationale or prior.author_id != getattr(actor, 'pk', actor)):
            raise ReportDecisionConflict('重复请求与原修订不一致。')
        return unit
    if unit.revision_number != expected_revision:
        raise ReportDecisionConflict('报告字段已变化，请刷新后核对。')
    state = report_revision_state(unit)
    if expected_source is not None and expected_source != report_source_token(unit, state):
        raise ReportDecisionConflict('报告依据或解析版本已变化，请刷新后核对。')
    if source_evidence.get('page_number') != unit.document_page.page_number or not source_evidence.get('polygon'):
        raise ValueError('必须指明本报告原件上的字段位置。')
    normalized_polygon(source_evidence['polygon'])
    before = state.identity
    if before.source_region and not contains_source_location(before.source_region, source_evidence['polygon']):
        raise ValueError('原件位置必须属于当前报告单元。')
    if action == 'CORRECT':
        after = _corrected(before, changes, source_evidence)
    else:
        if before.reason != 'report_revision_conflict':
            raise ReportDecisionConflict('当前没有待处理的报告修订冲突。')
        after = state.candidate if action == 'KEEP_REVISION' else _from_snapshot(unit.automatic)
    unit.revision_number += 1
    unit.save(update_fields=['revision_number'])
    LabReportRevision.objects.create(unit=unit, author=actor, sequence=unit.revision_number, operation_id=operation_id,
        before=_snapshot(before), after=_snapshot(after), source_evidence=source_evidence, rationale=rationale,
        action=action, inherited_from=state.revision)
    report_relations(patient)
    return unit


def _corrected(identity, changes, source):
    values = dict(changes)
    fields = dict(identity.fields)
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError('字段值不能为空。')
        fields[key] = [{**source, 'value': value, 'raw_text': value, 'confidence': 1, 'origin': 'USER'}]
    if 'sampled_at' in values:
        text = values['sampled_at']
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?', text):
            raise ValueError('采样时间必须包含原件提供的日期和时分。')
        values['sampled_at'] = datetime.fromisoformat(text)
        values['precision'] = 'SECOND' if len(text) == 19 else 'MINUTE'
        values['sampling_dates'] = (values['sampled_at'].date(),)
        values['time_source'] = ''
        if identity.reason.startswith('sampling_'):
            values.update(status='ACCEPTED', reason='')
    result = replace(identity, **values, fields=fields)
    reports = {item['value'] for item in fields.get('report_number', ())}
    institutions = {item['value'] for item in fields.get('institution', ())}
    patients = defaultdict(set)
    for item in fields.get('patient', ()):
        patients[item['value'][0]].add(item['value'][1])
    conflict = len(reports) > 1 or len(institutions) > 1 or any(len(items) > 1 for items in patients.values())
    reliable = bool(result.report_number and result.institution and not conflict
                    and all(item['confidence'] >= .9 for key in ('report_number', 'institution', 'patient') for item in fields.get(key, ())))
    if conflict and result.status == 'ACCEPTED':
        result = replace(result, status='REVIEW', reason='report_identity_conflict')
    elif result.reason == 'report_identity_conflict' and not conflict:
        result = replace(result, status='ACCEPTED' if result.sampled_at else 'REJECTED',
                         reason='' if result.sampled_at else 'sampling_datetime_missing')
    return replace(result, identity_reliable=reliable)
