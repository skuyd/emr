"""Confirm an explicitly previewed set of complete reports without changing values."""

from collections import defaultdict
import hashlib
import json
from uuid import UUID

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.patients.access import authorize_patient
from .models import LabConfirmationBatch, LabObservation
from .readmodels import effective_rows
from .report_reads import assign_report_groups
from .reports import ensure_historical_report_units, report_relations, report_source_token
from .revisions import RevisionConflict, _snapshot, append_revision


_SALT = 'labs.complete-report-confirmation'
_CONFIRMED = frozenset({'CONFIRM', 'CORRECT', 'KEEP_REVISION', 'USE_AUTOMATIC'})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _item(row):
    reasons = []
    if row.reported_error:
        reasons.append('识别有误')
    if row.revision_conflict:
        reasons.append('结果修订冲突')
    if row.report_identity.reason == 'report_revision_conflict':
        reasons.append('报告修订冲突')
    if row.report_conflict or row.report_identity.reason == 'report_identity_conflict':
        reasons.append('报告归属冲突')
    state = 'skipped' if reasons else 'confirmed' if row.review_state in _CONFIRMED else 'pending'
    return {'row': row, 'state': state, 'reasons': reasons,
            'label': {'skipped': '本次跳过', 'confirmed': '已确认', 'pending': '待确认'}[state]}


def confirmation_preview(patient):
    ensure_historical_report_units(patient)
    rows = effective_rows(patient, include_uncertain=True, include_invalid=True)
    assign_report_groups(patient, rows)
    relations = report_relations(patient)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.report_group_key].append(row)
    output = []
    for key, members in sorted(grouped.items()):
        units = {row.report_unit_id: row.report_unit for row in members if row.report_unit_id}
        source_keys = {unit.source_key for unit in units.values()}
        items = tuple(_item(row) for row in members)
        basis = {
            'units': sorted((str(unit.pk), report_source_token(unit)) for unit in units.values()),
            'items': sorted((str(item['row'].pk), item['state'], item['reasons']) for item in items),
            'rows': sorted((str(row.pk), str(row.parsing_version_id), row.revision_number,
                            str(row.applied_revision.pk) if row.applied_revision else None,
                            _snapshot(row), row.quality_issues,
                            str(row.evidence_id), row.evidence.source_text, row.evidence.polygon)
                           for row in members),
            'relations': sorted((str(item.pk), item.state, item.revision_number, item.evidence_fingerprint)
                                for item in relations if source_keys.intersection((item.left_key, item.right_key))),
        }
        fingerprint = _digest(basis)
        output.append({'key': key, 'items': items, 'units': tuple(units.values()),
                       'identity': members[0].report_identity,
                       'pending_count': sum(item['state'] == 'pending' for item in items),
                       'confirmed_count': sum(item['state'] == 'confirmed' for item in items),
                       'skipped_count': sum(item['state'] == 'skipped' for item in items),
                       'fingerprint': fingerprint,
                       'token': signing.dumps({'patient': str(patient.pk), 'key': key, 'fingerprint': fingerprint},
                                              salt=_SALT, compress=True)})
    return tuple(output)


@transaction.atomic
def confirm_reports(patient, actor, tokens, *, operation_id):
    access = authorize_patient(patient, actor, 'write', lock=True)
    patient, actor = access.patient, access.actor
    try:
        operation_id = UUID(str(operation_id))
        tokens = tuple(sorted(set(tokens)))
        if not tokens:
            raise ValueError()
        selections = [signing.loads(token, salt=_SALT) for token in tokens]
        if any(item['patient'] != str(patient.pk) for item in selections):
            raise PermissionDenied
        if len({item['key'] for item in selections}) != len(selections):
            raise ValueError()
    except (ValueError, TypeError, KeyError, signing.BadSignature):
        raise ValidationError('请选择报告并从当前预览提交确认。') from None
    request_fingerprint = _digest(tokens)
    prior = LabConfirmationBatch.objects.filter(patient=patient, author=actor, operation_id=operation_id).first()
    if prior:
        if prior.request_fingerprint != request_fingerprint:
            raise RevisionConflict('重复请求与原确认范围不一致，请刷新后重试。')
        return prior.result
    reports = {item['key']: item for item in confirmation_preview(patient)}
    selected = []
    for selection in selections:
        report = reports.get(selection['key'])
        if report is None or report['fingerprint'] != selection['fingerprint']:
            raise RevisionConflict('报告、结果或确认范围已变化，请刷新后重新核对。')
        selected.append(report)
    rows = {item['row'].pk: item['row'] for report in selected for item in report['items']}
    # Follow the shared patient -> upload batches -> document -> result lock order.
    for document_id in sorted({row.parsing_version.document_id for row in rows.values()}):
        document, _batches = lock_document_aggregate(document_id, patient_id=patient.pk)
        if document is None or document.deleted_at is not None:
            raise RevisionConflict('报告原件已变化，请刷新后重新核对。')
    locked = {row.pk: row for row in LabObservation.objects.select_for_update(of=('self',)).filter(pk__in=rows)
              .select_related('parsing_version__document', 'evidence', 'document_page').order_by('pk')}
    # Re-read after all aggregate locks so parsing activation cannot race the preview.
    current = {item['key']: item for item in confirmation_preview(patient)}
    for report in selected:
        if report['key'] not in current or current[report['key']]['fingerprint'] != report['fingerprint']:
            raise RevisionConflict('报告、结果或确认范围已变化，请刷新后重新核对。')
    actor = authorize_patient(patient, actor, 'write').actor
    result = {'report_count': len(selected), 'confirmed_count': 0, 'already_confirmed_count': 0,
              'skipped_count': 0, 'remaining_count': 0, 'reports': []}
    for report in selected:
        for item in report['items']:
            if item['state'] == 'pending':
                row = locked[item['row'].pk]
                append_revision(actor, row, action='CONFIRM', changes={}, expected_revision=item['row'].revision_number)
        counts = {'key': report['key'], 'confirmed_count': report['pending_count'],
                  'already_confirmed_count': report['confirmed_count'], 'skipped_count': report['skipped_count'],
                  'remaining_count': report['skipped_count']}
        result['reports'].append(counts)
        for name in ('confirmed_count', 'already_confirmed_count', 'skipped_count', 'remaining_count'):
            result[name] += counts[name]
    LabConfirmationBatch.objects.create(patient=patient, author=actor, operation_id=operation_id,
                                        request_fingerprint=request_fingerprint, result=result)
    return result
