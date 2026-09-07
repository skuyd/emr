"""Actual actors, patient/source locks and append-only glucose decisions."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import re
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
        lock_document_aggregate(source['source_document_id'], patient_id=patient.pk)
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
        if action == 'CORRECT' and record.source_kind in ('LAB_REPORT', 'NURSING'):
            from .sources import source_current
            if not source_current(record):
                raise GlucoseConflict('来源已变化，请先重新核对当前原件。')
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
                checked_changes = deepcopy(changes)
                if (record.source_kind in ('LAB_REPORT', 'NURSING') and isinstance(checked_changes, dict)
                        and checked_changes.get('timezone_origin') == 'SOURCE_EXPLICIT'):
                    printed = record.current_data['source'].get('report_context', {}).get('sample_time', {})
                    if printed.get('timezone_origin') != 'SOURCE_EXPLICIT' or checked_changes.get('timezone') != printed.get('timezone'):
                        checked_changes['timezone_origin'] = 'USER_CONFIRMED'
                data = normalize_payload(checked_changes, source_kind=record.source_kind,
                                         allow_imprecise=record.source_kind in ('LAB_REPORT', 'NURSING'))
                if record.source_kind in ('MANUAL', 'METER') and data['timezone_origin'] != 'USER_CONFIRMED':
                    raise GlucoseInputError('timezone_origin', '自测与手动记录的时区须由用户明确选择。')
                data['source'] = deepcopy(record.current_data['source'])
                data['field_origins'] = deepcopy(record.current_data['field_origins'])
                for field, keys in ORIGIN_FIELDS.items():
                    if any(data[key] != record.current_data[key] for key in keys):
                        data['field_origins'][field] = 'USER_CORRECTED'
                from .sources import apply_source_constraints
                apply_source_constraints(data)
                after['data'] = data
        return _append_revision(record, access.actor, action, after, now=instant)


def import_lab_record(patient, actor, observation_id, *, expected_source, checked_original, creation_key,
                      timezone_name='', confirm_timezone=False, utc_offset='', recheck=False, expected_revision=None, now=None):
    from .sources import lab_candidate, lock_lab

    with transaction.atomic():
        access = _write_access(patient, actor)
        key = _creation_key(creation_key)
        observation = lock_lab(access.patient, observation_id)
        candidate = lab_candidate(observation)
        if not isinstance(expected_source, str) or expected_source != candidate['source_fingerprint']:
            raise GlucoseConflict('来源已变化，请重新打开当前原件后核对。')
        if checked_original is not True:
            raise GlucoseInputError('checked_original', '请对照原件核对项目、标本、原值、单位和测量时间。')
        content = deepcopy(candidate['data'])
        if confirm_timezone is True:
            local = content['measured_local_raw']
            if utc_offset:
                if not isinstance(utc_offset, str) or not re.fullmatch(r'[+-][0-9]{2}:[0-9]{2}', utc_offset):
                    raise GlucoseInputError('utc_offset', '请选择有效的时区偏移。')
                if content['time_precision'] not in ('MINUTE', 'SECOND'):
                    raise GlucoseInputError('utc_offset', '未明确到分钟的日期不能添加时刻偏移。')
                local += utc_offset
            normalized = normalize_payload({'value': content['raw_value'], 'unit': content['raw_unit'],
                'measured_local': local, 'time_precision': content['time_precision'], 'timezone': timezone_name,
                'timezone_origin': 'USER_CONFIRMED', 'time_slot': content['time_slot'],
                'source_label': content['source_label'], 'notes': content['notes']}, source_kind='LAB_REPORT', allow_imprecise=True)
            content.update(normalized)
            content['field_origins']['timezone'] = 'USER_CONFIRMED'
        existing = GlucoseRecord.objects.select_for_update().filter(patient=access.patient, source_observation=observation).first()
        fingerprint = digest(content)
        key_record = GlucoseRecord.objects.filter(patient=access.patient, created_by=access.actor, creation_key=key).first()
        if key_record is not None and (key_record.source_observation_id != observation.pk
                                       or key_record.creation_fingerprint != fingerprint):
            raise GlucoseConflict('这次表单已保存不同内容，请刷新后核对原记录。')
        if existing is not None:
            if recheck is True:
                if type(expected_revision) is not int or expected_revision != existing.revision_number:
                    raise GlucoseConflict('记录已变化，请刷新后核对最新修订。')
                if existing.deleted_at is not None:
                    raise GlucoseConflict('记录已删除，请先撤销删除后再核对来源。')
                after = {'data': content, 'source_fingerprint': candidate['source_fingerprint'], 'deleted_at': None}
                return CreatedRecord(_append_revision(existing, access.actor, 'RECHECK', after, now=now or timezone.now()), False)
            if existing.source_fingerprint != candidate['source_fingerprint']:
                raise GlucoseConflict('这条来源已导入且发生变化，请在原记录上明确重新核对。')
            return CreatedRecord(existing, False)
        if recheck:
            raise GlucoseConflict('原记录已不可用，请从当前报告明确新建。')
        instant = now or timezone.now()
        record = GlucoseRecord(patient=access.patient, created_by=access.actor, updated_by=access.actor,
            creation_key=key, creation_fingerprint=fingerprint, source_kind='LAB_REPORT',
            source_document=observation.parsing_version.document, source_page=observation.document_page,
            source_parsing_version=observation.parsing_version, source_observation=observation,
            source_fingerprint=candidate['source_fingerprint'], original_data=deepcopy(content), current_data=content,
            **_time_columns(content), created_at=instant, updated_at=instant)
        record.full_clean()
        record.save()
        record_audit_event(access.actor.pk, 'glucose_record_created', record.pk, 'succeeded', 'lab_import', patient_id=access.patient.pk)
        return CreatedRecord(record, True)


def import_nursing_record(patient, actor, page_id, data, *, expected_source, checked_original, creation_key,
                          original_excerpt, measurement_scope, confirm_timezone=False, recheck_record_id=None,
                          expected_revision=None, now=None):
    from .sources import apply_source_constraints, lock_nursing_page, nursing_page_candidate

    with transaction.atomic():
        access = _write_access(patient, actor)
        key = _creation_key(creation_key)
        page = lock_nursing_page(access.patient, page_id)
        candidate = nursing_page_candidate(page)
        if not isinstance(expected_source, str) or expected_source != candidate['source_fingerprint']:
            raise GlucoseConflict('来源已变化，请重新打开当前原件后核对。')
        if checked_original is not True:
            raise GlucoseInputError('checked_original', '请对照护理原件核对本次录入内容。')
        if measurement_scope not in ('SINGLE', 'SUMMARY'):
            raise GlucoseInputError('measurement_scope', '请选择逐次测量或原文汇总；医嘱、剂量和计划不能作为测量。')
        if not isinstance(original_excerpt, str) or not original_excerpt.strip() or len(original_excerpt) > 2000:
            raise GlucoseInputError('original_excerpt', '请填写不超过 2000 字的原件对应文字。')
        if not isinstance(data, dict):
            raise GlucoseInputError('__all__', '记录内容无效。')
        entered = deepcopy(data)
        entered['timezone_origin'] = 'USER_CONFIRMED' if confirm_timezone is True else 'UNCONFIRMED'
        content = normalize_payload(entered, source_kind='NURSING', allow_imprecise=True)
        content['source'] = {**candidate['source'], 'original_excerpt': original_excerpt,
                             'measurement_scope': measurement_scope, 'transcription_origin': 'USER_TRANSCRIBED'}
        content['field_origins'] = dict.fromkeys(ORIGIN_FIELDS, 'USER_TRANSCRIBED')
        content['field_origins']['timezone'] = content['timezone_origin']
        apply_source_constraints(content)
        fingerprint = digest(content)
        existing = GlucoseRecord.objects.select_for_update().filter(patient=access.patient, created_by=access.actor, creation_key=key).first()
        if existing is not None and existing.creation_fingerprint != fingerprint:
            raise GlucoseConflict('这次表单已保存不同内容，请刷新后核对原记录。')
        instant = now or timezone.now()
        if recheck_record_id is not None:
            try:
                record = GlucoseRecord.objects.select_for_update().get(pk=recheck_record_id, patient=access.patient,
                    source_kind='NURSING', source_page=page, source_document=page.document)
            except (GlucoseRecord.DoesNotExist, ValidationError, ValueError, TypeError):
                raise PermissionDenied from None
            if type(expected_revision) is not int or record.revision_number != expected_revision:
                raise GlucoseConflict('记录已变化，请刷新后核对最新修订。')
            if record.deleted_at is not None:
                raise GlucoseConflict('记录已删除，请先撤销删除后再核对来源。')
            version_id = candidate['source']['parsing_version_id']
            record.source_parsing_version_id = UUID(version_id) if version_id else None
            record.save(update_fields=['source_parsing_version'])
            after = {'data': content, 'source_fingerprint': candidate['source_fingerprint'], 'deleted_at': None}
            return CreatedRecord(_append_revision(record, access.actor, 'RECHECK', after, now=instant), False)
        if existing is not None:
            if existing.source_fingerprint != candidate['source_fingerprint']:
                raise GlucoseConflict('来源已变化，请在原记录上明确重新核对。')
            return CreatedRecord(existing, False)
        record = GlucoseRecord(patient=access.patient, created_by=access.actor, updated_by=access.actor,
            creation_key=key, creation_fingerprint=fingerprint, source_kind='NURSING', source_document=page.document,
            source_page=page, source_parsing_version_id=candidate['source']['parsing_version_id'],
            source_fingerprint=candidate['source_fingerprint'], original_data=deepcopy(content), current_data=content,
            **_time_columns(content), created_at=instant, updated_at=instant)
        record.full_clean()
        record.save()
        record_audit_event(access.actor.pk, 'glucose_record_created', record.pk, 'succeeded', 'nursing_import', patient_id=access.patient.pk)
        return CreatedRecord(record, True)
