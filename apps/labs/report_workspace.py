"""One current report and its complete source scope for patient review."""

from collections import defaultdict
import hashlib
import json
from uuid import UUID

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.patients.access import authorize_patient

from .models import LabObservation, LabReportReviewEvent, LabReportUnit
from .manual_observations import add_manual_observation
from .readmodels import effective_rows
from .report_reads import read_report_identities, report_source_groups
from .reports import (_snapshot as _report_snapshot, correct_report, current_report_units,
                      ensure_historical_report_units, relation_has_conflict, report_relations, report_source_token)
from .revisions import RevisionConflict, _snapshot, append_revision


_SALT = 'labs.report-workspace'
_OBSERVATION_FIELDS = frozenset({'raw_name', 'raw_value', 'raw_unit', 'reference_range_raw',
                                 'report_flag_raw', 'specimen', 'method_raw', 'physiological_phase'})


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation_edits(edits):
    entries = edits.get('observations', [])
    if not isinstance(entries, list):
        raise ValidationError('指标修改格式无效。')
    output, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError('指标修改格式无效。')
        try:
            identity = UUID(str(entry.get('id')))
        except (TypeError, ValueError):
            raise ValidationError('指标编号无效。') from None
        revision = entry.get('expected_revision')
        action = entry.get('action', 'CORRECT')
        changes = entry.get('changes', {})
        reason = entry.get('reason', '')
        expected_keys = ({'id', 'expected_revision', 'changes'} if action == 'CORRECT' else
                         {'id', 'expected_revision', 'action', 'reason'} if action == 'EXCLUDE' else
                         {'id', 'expected_revision', 'action'} if action == 'RESTORE' else set())
        actual_keys = set(entry)
        keys_valid = actual_keys == expected_keys or (action in {'EXCLUDE', 'RESTORE'}
                                                       and actual_keys == expected_keys | {'changes'})
        if (identity in seen or isinstance(revision, bool) or not isinstance(revision, int)
                or not keys_valid or not isinstance(changes, dict)
                or (action == 'CORRECT' and (not changes or set(changes) - _OBSERVATION_FIELDS))
                or (action in {'EXCLUDE', 'RESTORE'} and set(changes) - _OBSERVATION_FIELDS)):
            raise ValidationError('指标修改内容无效或重复。')
        seen.add(identity)
        output.append((identity, revision, action, changes, reason))
    return tuple(output)


def _report_edits(edits):
    entries = edits.get('reports', [])
    if not isinstance(entries, list):
        raise ValidationError('报告信息修改格式无效。')
    output, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'unit_id', 'expected_revision', 'changes'}:
            raise ValidationError('报告信息修改格式无效。')
        try:
            identity = UUID(str(entry['unit_id']))
        except (TypeError, ValueError):
            raise ValidationError('报告来源编号无效。') from None
        revision, changes = entry['expected_revision'], entry['changes']
        if (identity in seen or isinstance(revision, bool) or not isinstance(revision, int)
                or not isinstance(changes, dict) or not changes
                or set(changes) - {'institution', 'sampled_at', 'report_number'}):
            raise ValidationError('报告信息修改内容无效或重复。')
        seen.add(identity)
        output.append((identity, revision, changes))
    return tuple(output)


def _report_resolutions(edits):
    entries = edits.get('report_resolutions', [])
    if not isinstance(entries, list):
        raise ValidationError('报告修订冲突处理格式无效。')
    output, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'unit_id', 'expected_revision', 'decision'}:
            raise ValidationError('报告修订冲突处理格式无效。')
        try:
            identity = UUID(str(entry['unit_id']))
        except (TypeError, ValueError):
            raise ValidationError('报告来源编号无效。') from None
        revision, decision = entry['expected_revision'], entry['decision']
        if (identity in seen or isinstance(revision, bool) or not isinstance(revision, int)
                or decision not in {'KEEP_REVISION', 'USE_AUTOMATIC'}):
            raise ValidationError('报告修订冲突处理内容无效或重复。')
        seen.add(identity)
        output.append((identity, revision, decision))
    return tuple(output)


def _additions(edits):
    entries = edits.get('additions', [])
    if not isinstance(entries, list):
        raise ValidationError('补录指标格式无效。')
    output = []
    for entry in entries:
        if not isinstance(entry, dict) or 'unit_id' not in entry:
            raise ValidationError('补录指标格式无效。')
        try:
            identity = UUID(str(entry['unit_id']))
        except (TypeError, ValueError):
            raise ValidationError('补录来源编号无效。') from None
        output.append((identity, {key: value for key, value in entry.items() if key != 'unit_id'}))
    return tuple(output)


def _resolutions(edits):
    entries = edits.get('resolutions', [])
    if not isinstance(entries, list):
        raise ValidationError('人工条目冲突处理格式无效。')
    output, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'manual_identity', 'winner_id'}:
            raise ValidationError('人工条目冲突处理格式无效。')
        try:
            identity, winner = UUID(str(entry['manual_identity'])), UUID(str(entry['winner_id']))
        except (TypeError, ValueError):
            raise ValidationError('人工条目冲突处理编号无效。') from None
        if identity in seen:
            raise ValidationError('同一人工条目只能处理一次。')
        seen.add(identity)
        output.append((identity, winner))
    return tuple(output)


def report_workspace(patient, *, selected_key=None):
    """Read reports from current report units, never from observation counts."""
    ensure_historical_report_units(patient)
    units = tuple(current_report_units(patient))
    relations = report_relations(patient)
    groups = {source: min(group) for source, group in report_source_groups(
        patient, units, relations=relations).items()}
    identities = read_report_identities(patient, units)
    grouped_units = defaultdict(list)
    for unit in units:
        grouped_units[groups[unit.source_key]].append(unit)
    grouped_rows = defaultdict(list)
    for row in effective_rows(patient, include_uncertain=True, include_invalid=True,
                              include_excluded=True, include_conflicted=True):
        if row.report_unit_id and row.report_unit.source_key in groups:
            grouped_rows[groups[row.report_unit.source_key]].append(row)
    reports = []
    for key, members in sorted(grouped_units.items(), key=lambda item: (min(unit.created_at for unit in item[1]), item[0])):
        members.sort(key=lambda unit: (unit.parsing_version.document.created_at, unit.document_page.page_number,
                                       unit.ordinal, str(unit.pk)))
        source_keys = {unit.source_key for unit in members}
        sources = tuple({'unit': unit, 'document': unit.parsing_version.document,
                         'page_number': unit.document_page.page_number,
                         'region': identities[unit.pk].source_region, 'identity': identities[unit.pk]}
                        for unit in members)
        source_order = {unit.pk: index for index, unit in enumerate(members)}
        rows = tuple(sorted(grouped_rows[key], key=lambda row: (
            source_order[row.report_unit_id], row.reading_order, str(row.pk))))
        touching_relations = [item for item in relations if source_keys.intersection((item.left_key, item.right_key))]
        basis = {
            'sources': [(str(unit.pk), unit.source_key, str(unit.parsing_version_id),
                         str(unit.document_page_id), unit.parsing_version.document.lifecycle_revision,
                         str(unit.parsing_version.document.updated_at), report_source_token(unit),
                         identities[unit.pk].status, identities[unit.pk].reason)
                        for unit in members],
            'rows': [(str(row.pk), str(row.parsing_version_id), row.revision_number,
                      str(row.applied_revision.pk) if row.applied_revision else None,
                      _snapshot(row), row.manual_conflict, row.quality_issues, str(row.evidence_id),
                      row.evidence.source_text, row.evidence.polygon)
                     for row in rows],
            'relations': sorted((str(item.pk), item.state, item.revision_number,
                                 item.evidence_fingerprint) for item in touching_relations),
        }
        fingerprint = _digest(basis)
        latest = LabReportReviewEvent.objects.filter(patient=patient, report_key=key, action='CONFIRM').order_by(
            '-created_at', '-pk').first()
        problems = []
        if any(relation_has_conflict(item) for item in touching_relations):
            problems.append('报告归属存在冲突')
        if any(identity.reason in {'report_identity_conflict', 'report_revision_conflict'}
               for identity in (identities[unit.pk] for unit in members)):
            problems.append('报告信息或修订存在冲突')
        if any(not row.excluded and (row.reported_error or row.revision_conflict or row.report_conflict
                                     or row.manual_conflict) for row in rows):
            problems.append('指标仍有识别错误或修订冲突')
        reports.append({'key': key, 'units': tuple(members), 'sources': sources, 'rows': rows,
                        'identity': identities[members[0].pk], 'fingerprint': fingerprint,
                        'confirmed': bool(latest and latest.final_fingerprint == fingerprint),
                        'problems': tuple(problems),
                        'token': signing.dumps({'patient': str(patient.pk), 'key': key,
                                               'fingerprint': fingerprint}, salt=_SALT, compress=True)})
    reports = tuple(reports)
    if selected_key is None:
        index = next((index for index, report in enumerate(reports) if not report['confirmed']),
                     0 if reports else None)
    else:
        index = next((index for index, report in enumerate(reports) if report['key'] == selected_key), None)
    return {'reports': reports, 'current': reports[index] if index is not None else None,
            'previous_key': reports[index - 1]['key'] if index is not None and index > 0 else None,
            'next_key': reports[index + 1]['key'] if index is not None and index + 1 < len(reports) else None}


@transaction.atomic
def submit_report_workspace(patient, actor, report_key, token, operation_id, edits, *, confirm=False):
    """Save or confirm one locked report from a signed current-source preview."""
    access = authorize_patient(patient, actor, 'write', lock=True)
    patient, actor = access.patient, access.actor
    try:
        operation_id = UUID(str(operation_id))
        selection = signing.loads(token, salt=_SALT)
        if selection['patient'] != str(patient.pk):
            raise PermissionDenied
        if selection['key'] != report_key:
            raise ValidationError('每次只能提交当前一份报告。')
        if not isinstance(edits, dict):
            raise ValueError()
    except (TypeError, ValueError, KeyError, signing.BadSignature):
        raise ValidationError('提交内容无效，请刷新当前报告后重试。') from None
    request_fingerprint = _digest({'key': report_key, 'token': token, 'edits': edits, 'confirm': confirm})
    prior = LabReportReviewEvent.objects.filter(patient=patient, author=actor, operation_id=operation_id).first()
    if prior:
        if prior.request_fingerprint != request_fingerprint:
            raise RevisionConflict('重复请求与原操作内容不一致。')
        return prior.result
    if set(edits) - {'observations', 'reports', 'report_resolutions', 'additions', 'resolutions'}:
        raise ValidationError('提交内容包含不属于本报告核对的操作。')
    observation_edits = _observation_edits(edits)
    report_edits = _report_edits(edits)
    report_resolutions = _report_resolutions(edits)
    if {identity for identity, _, _ in report_edits} & {identity for identity, _, _ in report_resolutions}:
        raise ValidationError('同一报告来源不能同时更正和处理修订冲突。')
    additions = _additions(edits)
    resolutions = _resolutions(edits)
    current = report_workspace(patient, selected_key=report_key)['current']
    if current is None or current['fingerprint'] != selection['fingerprint']:
        raise RevisionConflict('报告、来源或结果已变化，请刷新后重新核对。')
    allowed_rows = {row.pk for row in current['rows']}
    if any(identity not in allowed_rows for identity, _, _, _, _ in observation_edits):
        raise PermissionDenied('指标不属于当前报告。')
    allowed_units = {unit.pk for unit in current['units']}
    if any(identity not in allowed_units for identity, _, _ in report_edits):
        raise PermissionDenied('报告信息不属于当前报告。')
    if any(identity not in allowed_units for identity, _, _ in report_resolutions):
        raise PermissionDenied('报告信息不属于当前报告。')
    if any(identity not in allowed_units for identity, _ in additions):
        raise PermissionDenied('补录来源不属于当前报告。')
    for identity, winner in resolutions:
        group = [row for row in current['rows']
                 if row.manual_identity == identity or row.manual_counterpart == identity]
        if not group or winner not in {row.pk for row in group} or not any(row.manual_conflict for row in group):
            raise ValidationError('人工条目冲突已变化，请刷新后处理。')
    for document_id in sorted({unit.parsing_version.document_id for unit in current['units']}):
        document, _batches = lock_document_aggregate(document_id, patient_id=patient.pk)
        if document is None or document.deleted_at is not None:
            raise RevisionConflict('报告原件已变化，请刷新后重新核对。')
    tuple(LabReportUnit.objects.select_for_update(of=('self',)).filter(
        pk__in=[unit.pk for unit in current['units']]).order_by('pk').values_list('pk', flat=True))
    locked = {row.pk: row for row in LabObservation.objects.select_for_update(of=('self',)).filter(
        pk__in=[row.pk for row in current['rows']]).select_related('parsing_version__document', 'evidence',
                                                                   'document_page').order_by('pk')}
    current = report_workspace(patient, selected_key=report_key)['current']
    if current is None or current['fingerprint'] != selection['fingerprint']:
        raise RevisionConflict('报告、来源或结果已变化，请刷新后重新核对。')
    actor = authorize_patient(patient, actor, 'write').actor
    sources = {source['unit'].pk: source for source in current['sources']}
    for identity, revision, changes in report_edits:
        source = sources[identity]
        polygon = source['region'] or ((0, 0), (1, 0), (1, 1), (0, 1))
        correct_report(patient, actor, identity, changes, expected_revision=revision,
            source_evidence={'page_number': source['page_number'], 'polygon': [list(point) for point in polygon],
                             'origin': 'MANUAL'},
            rationale='对照本报告原件转录', operation_id=f'{operation_id}:{identity}',
            expected_source=report_source_token(source['unit']))
    for identity, revision, decision in report_resolutions:
        source = sources[identity]
        polygon = source['region'] or ((0, 0), (1, 0), (1, 1), (0, 1))
        correct_report(patient, actor, identity, {}, expected_revision=revision,
            source_evidence={'page_number': source['page_number'], 'polygon': [list(point) for point in polygon],
                             'origin': 'MANUAL'},
            rationale='对照本报告原件处理修订冲突', operation_id=f'{operation_id}:{identity}',
            expected_source=report_source_token(source['unit']), action=decision)
    for identity, revision, action, changes, reason in observation_edits:
        if action in {'EXCLUDE', 'RESTORE'} and changes:
            append_revision(actor, locked[identity], action='CORRECT', changes=changes,
                            expected_revision=revision)
            revision += 1
        append_revision(actor, locked[identity], action=action,
                        changes=changes if action == 'CORRECT' else {},
                        expected_revision=revision, exclusion_reason=reason)
    for identity, winner in resolutions:
        group = [row for row in current['rows']
                 if row.manual_identity == identity or row.manual_counterpart == identity]
        for row in group:
            append_revision(actor, locked[row.pk], action='RECONCILE', changes={},
                            expected_revision=row.revision_number, resolution_keep=row.pk == winner)
    units = {unit.pk: unit for unit in current['units']}
    for identity, values in additions:
        row = add_manual_observation(units[identity], actor, values)
        locked[row.pk] = row
    edited = report_workspace(patient, selected_key=report_key)['current']
    if edited is None:
        raise RevisionConflict('报告来源已变化，请刷新后重新核对。')
    if confirm and edited['problems']:
        raise ValidationError('本报告仍有待处理问题，保存可用，确认前请先处理。')
    if confirm:
        for row in edited['rows']:
            if not row.excluded and row.review_state != 'CONFIRM':
                append_revision(actor, locked[row.pk], action='CONFIRM', changes={},
                                expected_revision=row.revision_number)
    final = report_workspace(patient, selected_key=report_key)['current']
    if final is None or confirm and final['problems']:
        raise RevisionConflict('报告内容或来源已变化，请刷新后重新核对。')
    basis = {'sources': [str(unit.pk) for unit in final['units']],
             'retained': [str(row.pk) for row in final['rows'] if not row.excluded],
             'excluded': [{'id': str(row.pk), 'reason': row.exclusion_reason}
                          for row in final['rows'] if row.excluded],
             'report': [{'unit': str(source['unit'].pk), 'identity': _report_snapshot(source['identity'])}
                        for source in final['sources']],
             'fingerprint': final['fingerprint']}
    result = {'report_key': report_key, 'confirmed': bool(confirm), 'fingerprint': final['fingerprint']}
    LabReportReviewEvent.objects.create(patient=patient, author=actor, operation_id=operation_id,
        request_fingerprint=request_fingerprint, report_key=report_key,
        action='CONFIRM' if confirm else 'SAVE', basis=basis,
        final_fingerprint=final['fingerprint'], result=result)
    return result
