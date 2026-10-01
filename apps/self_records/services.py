"""Patient/actor locks protect current input and retry identity."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, time
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.access import Capability, authorize_patient

from .models import DailyRecord
from .payloads import InvalidRecord, normalize_payload


class RecordConflict(ValueError):
    pass


@dataclass(frozen=True)
class CreatedRecord:
    record: DailyRecord
    created: bool


def _write_access(patient, actor):
    # Account deletion and invitation acceptance use this same lock order.
    # NO KEY UPDATE permits unrelated FK inserts while serializing lifecycle.
    account = Account.objects.select_for_update(no_key=True).filter(pk=getattr(actor, 'pk', actor), is_active=True).first()
    if account is None:
        raise PermissionDenied
    return authorize_patient(patient, account, Capability.WRITE, lock=True)


def create_record(patient, actor, data, *, creation_key, now=None):
    with transaction.atomic():
        access = _write_access(patient, actor)
        try:
            key = UUID(str(creation_key))
        except (TypeError, ValueError, AttributeError):
            raise InvalidRecord('creation_key', '表单标识无效，请重新打开后保存。') from None
        content = normalize_payload(data)
        fingerprint = digest(content)
        existing = DailyRecord.objects.filter(patient=access.patient, created_by=access.actor, creation_key=key).first()
        if existing is not None:
            if existing.creation_fingerprint != fingerprint:
                raise RecordConflict('这次表单已保存不同内容，请刷新后更正原记录或新建一条。')
            if existing.deleted_at is not None:
                raise RecordConflict('这条记录已删除，不能通过重试恢复。')
            return CreatedRecord(existing, False)
        instant = now or timezone.now()
        record = DailyRecord.objects.create(
            patient=access.patient, created_by=access.actor, updated_by=access.actor,
            creation_key=key, creation_fingerprint=fingerprint,
            original_data=deepcopy(content), current_data=content,
            kind=content['kind'], measured_at=None,
            record_date=date.fromisoformat(content['record_date']),
            record_time=time.fromisoformat(content['record_time']) if content['record_time'] else None,
            created_at=instant, updated_at=instant,
        )
        record_audit_event(access.actor.pk, 'self_record_created', record.pk, 'succeeded', patient_id=access.patient.pk)
        return CreatedRecord(record, True)


def revise_record(patient, actor, record_id, *, action, expected_revision, changes=None, now=None):
    with transaction.atomic():
        access = _write_access(patient, actor)
        try:
            record = DailyRecord.objects.select_for_update().filter(pk=record_id, patient=access.patient).first()
        except (ValidationError, ValueError, TypeError):
            raise PermissionDenied from None
        if record is None:
            raise PermissionDenied
        if type(expected_revision) is not int or expected_revision != record.revision_number:
            raise RecordConflict('记录已变化，请刷新并核对最新内容。')
        if action not in {'CORRECT', 'DELETE'}:
            raise InvalidRecord('action', '请选择有效的记录操作。')
        if action != 'CORRECT' and changes is not None:
            raise InvalidRecord('action', '请使用更正操作修改记录内容。')
        if record.deleted_at is not None:
            raise RecordConflict('记录已删除，不能再更正或删除。')
        instant = now or timezone.now()
        if action == 'CORRECT':
            content = normalize_payload(changes)
            if content['kind'] != record.kind:
                raise InvalidRecord('kind', '更正时不能改变记录类型。')
            record.current_data = content
            record.original_data = deepcopy(content)
            record.record_date = date.fromisoformat(content['record_date'])
            record.record_time = time.fromisoformat(content['record_time']) if content['record_time'] else None
            record.measured_at = None
        else:
            record.deleted_at = instant
            record.current_data = {}
            record.original_data = {}
        record.revision_number += 1
        record.updated_by = access.actor
        record.updated_at = instant
        record.save(update_fields=['current_data', 'original_data', 'measured_at', 'record_date', 'record_time',
                                   'deleted_at', 'revision_number', 'updated_by', 'updated_at'])
        from .lifecycle import invalidate_record_outputs
        invalidate_record_outputs(record)
        record_audit_event(access.actor.pk, 'self_record_revised', record.pk, 'succeeded', action.lower(), patient_id=access.patient.pk)
        return record
