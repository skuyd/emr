"""Actual actors, patient/source locks and append-only glucose decisions."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.documents.locking import lock_document_aggregate
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.access import Capability, authorize_patient

from .lifecycle import invalidate_record_outputs
from .models import GlucoseRecord, GlucoseRevision
from .payloads import GlucoseInputError, normalize_payload


class GlucoseConflict(ValueError):
    pass


@dataclass(frozen=True)
class CreatedRecord:
    record: GlucoseRecord
    created: bool


ORIGIN_FIELDS = {
    'value': ('raw_value',), 'unit': ('raw_unit',),
    'time': ('measured_local_raw', 'time_precision'), 'timezone': ('timezone', 'timezone_origin'),
    'time_slot': ('time_slot',), 'source_label': ('source_label',), 'notes': ('notes',),
}


def _write_access(patient, actor):
    account = Account.objects.select_for_update(no_key=True).filter(pk=getattr(actor, 'pk', actor), is_active=True).first()
    if account is None:
        raise PermissionDenied
    return authorize_patient(patient, account, Capability.WRITE, lock=True)


def _creation_key(value):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise GlucoseInputError('creation_key', '表单标识无效，请重新打开后保存。') from None


def _time_columns(data):
    return dict(measured_at=datetime.fromisoformat(data['measured_at']) if data['measured_at'] else None,
                measured_date=date.fromisoformat(data['measured_date']) if data['measured_date'] else None,
                time_precision=data['time_precision'])


def _state(record):
    return {'data': deepcopy(record.current_data), 'source_fingerprint': record.source_fingerprint,
            'deleted_at': record.deleted_at.isoformat() if record.deleted_at else None}


def _lock_record(patient, record_id):
    try:
        source = GlucoseRecord.objects.filter(patient=patient, pk=record_id).values('source_document_id').first()
    except (ValidationError, ValueError, TypeError):
        raise PermissionDenied from None
    if source is None:
        raise PermissionDenied
    if source['source_document_id']:
        # The immutable source binding is read before taking its child lock.
        lock_document_aggregate(source['source_document_id'], patient.pk)
    return GlucoseRecord.objects.select_for_update().get(pk=record_id, patient=patient)


def _append_revision(record, actor, action, after, *, now):
    before = _state(record)
    GlucoseRevision.objects.create(record=record, author=actor, sequence=record.revision_number + 1,
                                   action=action, before=before, after=deepcopy(after), created_at=now)
    record.current_data = deepcopy(after['data'])
    record.source_fingerprint = after['source_fingerprint']
    record.deleted_at = datetime.fromisoformat(after['deleted_at']) if after['deleted_at'] else None
    for field, value in _time_columns(record.current_data).items():
        setattr(record, field, value)
    record.revision_number += 1
    record.updated_by = actor
    record.updated_at = now
    record.save(update_fields=['current_data', 'source_fingerprint', 'deleted_at', 'measured_at', 'measured_date',
                               'time_precision', 'revision_number', 'updated_by', 'updated_at'])
    invalidate_record_outputs(record)
    record_audit_event(actor.pk, 'glucose_record_revised', record.pk, 'succeeded', action.lower(), patient_id=record.patient_id)
    return record


def create_record(patient, actor, data, *, creation_key, source_kind='MANUAL', now=None):
    with transaction.atomic():
        access = _write_access(patient, actor)
        key = _creation_key(creation_key)
        if source_kind not in ('MANUAL', 'METER'):
            raise GlucoseInputError('source_kind', '检验与护理来源须从实际原件核对导入。')
        content = normalize_payload(data, source_kind=source_kind)
        if content['timezone_origin'] != 'USER_CONFIRMED':
            raise GlucoseInputError('timezone_origin', '自测与手动记录的时区须由用户明确选择。')
        content['field_origins'] = dict.fromkeys(ORIGIN_FIELDS, 'USER_ENTERED')
        content['source'] = {}
        fingerprint = digest(content)
        existing = GlucoseRecord.objects.filter(patient=access.patient, created_by=access.actor, creation_key=key).first()
        if existing is not None:
            if existing.creation_fingerprint != fingerprint:
                raise GlucoseConflict('这次表单已保存不同内容，请刷新后更正原记录或新建一条。')
            return CreatedRecord(existing, False)
        instant = now or timezone.now()
        record = GlucoseRecord(patient=access.patient, created_by=access.actor, updated_by=access.actor,
            creation_key=key, creation_fingerprint=fingerprint, source_kind=source_kind,
            original_data=deepcopy(content), current_data=content, **_time_columns(content),
            created_at=instant, updated_at=instant)
        record.full_clean()
        record.save()
        record_audit_event(access.actor.pk, 'glucose_record_created', record.pk, 'succeeded', patient_id=access.patient.pk)
        return CreatedRecord(record, True)


def revise_record(patient, actor, record_id, *, action, expected_revision, changes=None, now=None):
    with transaction.atomic():
        access = _write_access(patient, actor)
        record = _lock_record(access.patient, record_id)
        if type(expected_revision) is not int or expected_revision != record.revision_number:
            raise GlucoseConflict('记录已变化，请刷新并核对最新内容。')
        if action not in ('CORRECT', 'DELETE', 'UNDO'):
            raise GlucoseInputError('action', '请选择有效的记录操作。')
        if action != 'CORRECT' and changes is not None:
            raise GlucoseInputError('action', '请使用更正操作修改记录内容。')
        after = _state(record)
        instant = now or timezone.now()
        if action == 'UNDO':
            previous = record.revisions.filter(sequence=record.revision_number).first()
            if previous is None:
                raise GlucoseConflict('没有可撤销的最近修订，首次记录可通过删除撤回。')
            after = deepcopy(previous.before)
        else:
            if record.deleted_at is not None:
                raise GlucoseConflict('记录已删除，请先撤销删除后再更正。')
            if action == 'DELETE':
                after['deleted_at'] = instant.isoformat()
            else:
                data = normalize_payload(changes, source_kind=record.source_kind,
                                         allow_imprecise=record.source_kind in ('LAB_REPORT', 'NURSING'))
                if record.source_kind in ('MANUAL', 'METER') and data['timezone_origin'] != 'USER_CONFIRMED':
                    raise GlucoseInputError('timezone_origin', '自测与手动记录的时区须由用户明确选择。')
                data['source'] = deepcopy(record.current_data['source'])
                data['field_origins'] = deepcopy(record.current_data['field_origins'])
                for field, keys in ORIGIN_FIELDS.items():
                    if any(data[key] != record.current_data[key] for key in keys):
                        data['field_origins'][field] = 'USER_CORRECTED'
                after['data'] = data
        return _append_revision(record, access.actor, action, after, now=instant)
