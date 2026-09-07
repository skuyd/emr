"""Patient/actor locks protect original input, revisions and retry identity."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.facts.readmodels import digest
from apps.patients.access import Capability, authorize_patient

from .models import DailyRecord, DailyRecordRevision
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


def _state(record):
    return {'data': deepcopy(record.current_data), 'deleted_at': record.deleted_at.isoformat() if record.deleted_at else None}


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
            return CreatedRecord(existing, False)
        instant = now or timezone.now()
        record = DailyRecord.objects.create(
            patient=access.patient, created_by=access.actor, updated_by=access.actor,
            creation_key=key, creation_fingerprint=fingerprint,
            original_data=deepcopy(content), current_data=content,
            kind=content['kind'], measured_at=datetime.fromisoformat(content['measured_at']),
            created_at=instant, updated_at=instant,
        )
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
        if action not in {'CORRECT', 'DELETE', 'UNDO'}:
            raise InvalidRecord('action', '请选择有效的记录操作。')
        if action != 'CORRECT' and changes is not None:
            raise InvalidRecord('action', '请使用更正操作修改记录内容。')
        before = _state(record)
        after = deepcopy(before)
        instant = now or timezone.now()
        if action == 'UNDO':
            previous = record.revisions.filter(sequence=record.revision_number).first()
            if previous is None:
                raise RecordConflict('没有可撤销的最近修订，首次记录可通过删除撤回。')
            after = deepcopy(previous.before)
        else:
            if record.deleted_at is not None:
                raise RecordConflict('记录已删除，请先撤销删除后再更正。')
            if action == 'CORRECT':
                after['data'] = normalize_payload(changes)
            else:
                after['deleted_at'] = instant.isoformat()
        DailyRecordRevision.objects.create(
            record=record, author=access.actor, sequence=record.revision_number + 1,
            action=action, before=before, after=after, created_at=instant,
        )
        record.current_data = deepcopy(after['data'])
        record.kind = record.current_data['kind']
        record.measured_at = datetime.fromisoformat(record.current_data['measured_at'])
        record.deleted_at = datetime.fromisoformat(after['deleted_at']) if after['deleted_at'] else None
        record.revision_number += 1
        record.updated_by = access.actor
        record.updated_at = instant
        record.save(update_fields=['current_data', 'kind', 'measured_at', 'deleted_at', 'revision_number', 'updated_by', 'updated_at'])
        return record
