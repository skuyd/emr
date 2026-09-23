"""Patient-scoped temporary admission, before immutable archive creation."""

from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import timedelta
import uuid

from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone

from apps.labs.report_identity import recognize_report_units, resolve_continuation_times, matching_report_identity
from apps.labs.reports import _snapshot, _from_snapshot, _plain, result_identity
from apps.patients.access import authorize_patient, owner_actor
from apps.processing.value_objects import OcrPage, OcrRegion
from .batches import refresh_batch_state
from .deduplication import find_exact_duplicate
from .inspection import inspect_upload
from .models import Document, UploadBatch, UploadItem, UploadIntake, UploadItemStatus
from .quotas import check_upload_quota, lock_patient_quota
from .services import (
    UploadOutcome, UploadOutcomeKind, UploadResourceNotFound, UploadStateConflict,
    _batch_proposal, _validate_artifacts, _safe_dispatch, finalize_upload,
)
from .storage import StagedObject
from .similarity import find_possible_duplicate


def decode_pages(values):
    return tuple(OcrPage(**{**page, 'regions': tuple(OcrRegion(**region) for region in page['regions'])})
                 for page in values)


def register_intake(patient, batch_id, item_id, inspected, staged, store, *, actor=None, dispatch=None, display_filename=None):
    _validate_artifacts(inspected, staged)
    with transaction.atomic():
        access = authorize_patient(patient, owner_actor(patient, actor), 'write', lock=True)
        quota = lock_patient_quota(patient)
        batch = UploadBatch.objects.select_for_update().filter(pk=batch_id, patient=patient).first()
        item = UploadItem.objects.select_for_update().filter(pk=item_id, batch=batch).first() if batch else None
        if item is None:
            raise UploadResourceNotFound()
        if display_filename is not None:
            from .models import sanitize_display_filename
            item.display_filename = sanitize_display_filename(display_filename)
            item.save(update_fields=['display_filename', 'updated_at'])
        if item.status in {UploadItemStatus.CREATED, UploadItemStatus.EXACT_DUPLICATE} or find_exact_duplicate(patient, inspected.sha256):
            return finalize_upload(patient, batch_id, item_id, inspected, staged, store, actor=actor)
        if item.status not in {UploadItemStatus.PENDING, UploadItemStatus.UPLOADING, UploadItemStatus.UPLOAD_FAILED}:
            raise UploadStateConflict()
        if UploadIntake.objects.filter(item=item, cleanup_pending=True).exists():
            raise UploadStateConflict()
        proposal, batch_bytes = _batch_proposal(batch, item, inspected)
        check_upload_quota(patient, proposal, quota=quota, exclude_item_id=item.pk)
        UploadIntake.objects.update_or_create(item=item, defaults={'state': 'QUEUED', 'staged': asdict(staged),
            'promotion_key': '',
            'requested_by': access.actor, 'access_revision': access.membership.revision,
            'content_type': inspected.content_type, 'recognition': {}, 'lease_token': None,
            'heartbeat_at': None, 'next_attempt_at': None, 'attempt_count': 0, 'cleanup_pending': False})
        item.status, item.error_code, item.validity = UploadItemStatus.VALIDATING, '', {}
        item.byte_size, item.page_count = inspected.byte_size, inspected.page_count
        item.save(update_fields=['status', 'error_code', 'validity', 'byte_size', 'page_count', 'updated_at'])
        batch.file_count, batch.page_count, batch.byte_size = proposal.batch_files, proposal.batch_pages, batch_bytes
        batch.save(update_fields=['file_count', 'page_count', 'byte_size', 'updated_at'])
        refresh_batch_state(batch)
        if dispatch:
            transaction.on_commit(lambda: _safe_dispatch(dispatch, item.pk))
    return UploadOutcome(UploadOutcomeKind.VALIDATING, None, item.pk, None,
                         find_possible_duplicate(patient, inspected.perceptual_hash), saved=False)


def cleanup_intake(item_id, store):
    """Only staging is disposable here; failed cleanup remains private and retryable."""
    with transaction.atomic():
        intake = UploadIntake.objects.select_for_update().filter(item_id=item_id, cleanup_pending=True).first()
        if intake is None:
            return True
        try:
            if intake.promotion_key and not Document.objects.filter(original_object_key=intake.promotion_key).exists():
                if intake.promotion_key != f'originals/{intake.item_id.hex}':
                    raise UploadStateConflict()
                # A lost worker cannot retain the S3 version id returned by the
                # promotion. Purge this reserved, unreferenced key's versions.
                store.delete(intake.promotion_key)
            if intake.staged:
                store.delete(StagedObject(**intake.staged))
        except Exception:
            intake.next_attempt_at = timezone.now() + timedelta(seconds=60)
            intake.save(update_fields=['next_attempt_at', 'updated_at'])
            return False
        intake.staged, intake.cleanup_pending, intake.next_attempt_at, intake.promotion_key = {}, False, None, ''
        intake.save(update_fields=['staged', 'cleanup_pending', 'next_attempt_at', 'promotion_key', 'updated_at'])
        return True


class _RecognitionContext:
    def __init__(self, item_id, token):
        self.item_id, self.token = item_id, token

    def heartbeat(self, stage=None):
        if not UploadIntake.objects.filter(item_id=self.item_id, lease_token=self.token,
                state='RECOGNIZING').update(heartbeat_at=timezone.now()):
            raise UploadStateConflict()


def run_intake(item_id, pipeline, store, *, dispatch=None):
    candidate = UploadIntake.objects.select_related('item__batch__patient').filter(item_id=item_id).first()
    if candidate is None:
        return 'NOT_FOUND'
    if candidate.state != 'SETTLED':
        try:
            _authorize_intake(candidate)
        except PermissionDenied:
            _cancel_intake(candidate, store, 'access_revoked')
            return 'SETTLED'
    with transaction.atomic():
        intake = UploadIntake.objects.select_for_update().select_related('item__batch').filter(item_id=item_id).first()
        if intake is None:
            return 'NOT_FOUND'
        batch_id = intake.item.batch_id
        if intake.state == 'SETTLED':
            cleanup_intake(item_id, store)
            return 'SETTLED'
        if intake.state == 'RECOGNIZING':
            return 'BUSY'
        if intake.state == 'QUEUED':
            if intake.next_attempt_at and intake.next_attempt_at > timezone.now():
                return 'RETRY_SCHEDULED'
            intake.state, intake.lease_token = 'RECOGNIZING', uuid.uuid4()
            intake.heartbeat_at = timezone.now()
            intake.attempt_count += 1
            intake.save(update_fields=['state', 'lease_token', 'heartbeat_at', 'attempt_count', 'updated_at'])
            token = intake.lease_token
        else:
            token = None
    if token:
        try:
            with store.open_staging(StagedObject(**intake.staged)) as source:
                pages, warnings, material = pipeline.recognize_upload(source, intake.content_type,
                    _RecognitionContext(item_id, token))
            units = recognize_report_units(pages, pipeline.dictionary)
            recognition = {'pages': _plain([asdict(page) for page in pages]), 'warnings': list(warnings),
                           'material': material, 'units': [_snapshot(unit) for unit in units]}
            changed = UploadIntake.objects.filter(item_id=item_id, state='RECOGNIZING', lease_token=token).update(
                recognition=recognition, state='READY', lease_token=None, heartbeat_at=timezone.now(), next_attempt_at=None)
            if not changed:
                return 'STALE'
        except Exception:
            # Recognition/storage failures are retryable processing failures, never a medical rejection.
            changed = UploadIntake.objects.filter(item_id=item_id, state='RECOGNIZING', lease_token=token).update(
                state='QUEUED', lease_token=None, next_attempt_at=timezone.now() + timedelta(seconds=60))
            if not changed:
                return 'STALE'
            if intake.attempt_count >= 4:
                _cancel_intake(intake, store, 'upload_service_unavailable')
                return settle_batch(batch_id, pipeline, store, dispatch=dispatch)
            return 'RETRY_SCHEDULED'
    try:
        return settle_batch(batch_id, pipeline, store, dispatch=dispatch)
    except PermissionDenied:
        _cancel_intake(UploadIntake.objects.get(item_id=item_id), store, 'access_revoked')
        return 'SETTLED'


def _authorize_intake(intake):
    access = authorize_patient(intake.item.batch.patient_id, intake.requested_by_id, 'write')
    if access.membership.revision != intake.access_revision:
        raise PermissionDenied
    return access


def _cancel_intake(intake, store, code):
    from apps.patients.models import Patient

    with transaction.atomic():
        Patient.objects.select_for_update().get(pk=intake.item.batch.patient_id)
        item = UploadItem.objects.select_for_update().get(pk=intake.item_id)
        if item.document_id:
            return
        UploadIntake.objects.filter(item=item).update(state='SETTLED', recognition={},
            lease_token=None, cleanup_pending=True)
        item.status, item.error_code, item.validity = 'UPLOAD_FAILED', code, {}
        item.save(update_fields=['status', 'error_code', 'validity', 'updated_at'])
        batch = UploadBatch.objects.select_for_update().get(pk=item.batch_id)
        refresh_batch_state(batch)
    cleanup_intake(item.pk, store)


def _unit_keys(intake):
    counts = Counter()
    for values in intake.recognition['units']:
        unit = _from_snapshot(values)
        counts[unit.page_number] += 1
        yield f'{intake.item_id}:{unit.page_number}:{unit.ordinal or counts[unit.page_number]}', unit


def _continuation_conflicts(items, intakes, dictionary):
    from apps.labs.extraction import extract_observations

    pages = {str(intake.item_id): decode_pages(intake.recognition['pages']) for intake in intakes}
    results = {}

    def values(key, unit):
        if key not in results:
            selected = tuple(replace(page, regions=tuple(region for region in page.regions
                if unit.start_order <= region.reading_order <= unit.end_order))
                for page in pages[key.split(':')[0]] if page.page_number == unit.page_number)
            grouped = defaultdict(set)
            for row in extract_observations(selected, dictionary):
                grouped[(row.standard_code, row.specimen)].add(result_identity(row))
            results[key] = grouped
        return results[key]

    conflicts = set()
    for key, unit in items:
        if unit.page_index is None or unit.page_index <= 1:
            continue
        for other_key, other in items:
            if other.page_index != 1 or not matching_report_identity(key, unit, other_key, other):
                continue
            left, right = values(key, unit), values(other_key, other)
            if any(left[field] != right[field] or len(left[field]) != 1 or None in left[field]
                   for field in left.keys() & right.keys()):
                conflicts.add(frozenset((key, other_key)))
    return conflicts


def _admission(intake, resolved):
    recognition = dict(intake.recognition)
    units = [resolved[key] for key, _ in _unit_keys(intake)]
    pages = decode_pages(recognition['pages'])
    lab_pages = {unit.page_number for unit in units}
    has_nonlab = any(page.regions and page.page_number not in lab_pages for page in pages)
    accepted = [unit for unit in units if unit.status != 'REJECTED']
    rejected = [unit for unit in units if unit.status == 'REJECTED']
    admitted = bool(accepted or has_nonlab or not units)
    counts = Counter(unit.status for unit in units)
    validity = {'status': ('REVIEW' if counts['REVIEW'] else 'ACCEPTED') if admitted else 'REJECTED',
        'accepted': counts['ACCEPTED'], 'review': counts['REVIEW'], 'rejected': counts['REJECTED'],
        'shared_original_retained': bool(admitted and rejected),
        'units': [{'page_number': unit.page_number, 'ordinal': unit.ordinal, 'status': unit.status,
                   'reason': unit.reason} for unit in units]}
    if not admitted:
        return validity, {}
    # All downstream parsers see only admitted regions. Rejected text, metadata
    # and numerical values never enter permanent OCR, facts or lab extraction.
    sanitized = [replace(page, regions=tuple(region for region in page.regions if not any(
        unit.page_number == page.page_number and unit.start_order <= region.reading_order <= unit.end_order
        for unit in rejected))) for page in pages]
    recognition.update(admitted=True, pages=_plain([asdict(page) for page in sanitized]),
                       units=[_snapshot(unit) for unit in accepted])
    return validity, recognition


def settle_batch(batch_id, pipeline, store, *, dispatch=None):
    with transaction.atomic():
        batch = UploadBatch.objects.select_related('patient', 'created_by').get(pk=batch_id)
        lock_patient_quota(batch.patient)
        batch = UploadBatch.objects.select_for_update().get(pk=batch_id)
        items = list(batch.items.select_for_update().all())
        intakes = list(UploadIntake.objects.select_for_update().filter(item__batch=batch).select_related('item'))
        if any(item.status in {'PENDING', 'UPLOADING'} for item in items) or any(
                intake.state in {'QUEUED', 'RECOGNIZING'} for intake in intakes):
            return 'WAITING_BATCH'
        ready = [intake for intake in intakes if intake.state == 'READY' and not intake.recognition.get('admitted')]
        identities = tuple(pair for intake in ready for pair in _unit_keys(intake))
        resolved = resolve_continuation_times(identities,
            conflicting_pairs=_continuation_conflicts(identities, ready, pipeline.dictionary))
        for intake in ready:
            validity, recognition = _admission(intake, resolved)
            intake.item.validity = validity
            intake.recognition = recognition
            if validity['status'] == 'REJECTED':
                intake.item.status = UploadItemStatus.REJECTED
                intake.item.error_code = validity['units'][0]['reason']
                intake.state, intake.cleanup_pending = 'SETTLED', True
            else:
                intake.promotion_key = f'originals/{intake.item_id.hex}'
            intake.item.save(update_fields=['validity', 'status', 'error_code', 'updated_at'])
            intake.save(update_fields=['recognition', 'state', 'cleanup_pending', 'promotion_key', 'updated_at'])
        refresh_batch_state(batch)
    # Commit the admission decision before promotion. Each finalizer owns its
    # transaction/compensation; a crash can resume from the durable decision.
    for intake in UploadIntake.objects.filter(item__batch_id=batch_id).select_related('item__batch__patient', 'requested_by'):
        if intake.state == 'READY' and intake.recognition.get('admitted'):
            item = intake.item
            if not item.document_id:
                try:
                    _authorize_intake(intake)
                except PermissionDenied:
                    _cancel_intake(intake, store, 'access_revoked')
                    continue
                with store.open_staging(StagedObject(**intake.staged)) as source:
                    with inspect_upload(source, item.display_filename) as inspected:
                        outcome = finalize_upload(item.batch.patient, batch_id, item.pk, inspected,
                            StagedObject(**intake.staged), store, actor=intake.requested_by, dispatch=dispatch)
                        if outcome.kind == UploadOutcomeKind.EXACT_DUPLICATE:
                            UploadIntake.objects.filter(item=item).update(recognition={})
            UploadIntake.objects.filter(item_id=item.pk, state='READY').update(state='SETTLED', cleanup_pending=True)
        cleanup_intake(intake.item_id, store)
    return 'SETTLED'


def recover_intakes(*, dispatch=None, store=None, now=None):
    now = now or timezone.now()
    UploadIntake.objects.filter(state='RECOGNIZING', heartbeat_at__lt=now - timedelta(minutes=15)).update(
        state='QUEUED', lease_token=None, next_attempt_at=None)
    due = UploadIntake.objects.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
    if store:
        for item_id in due.filter(cleanup_pending=True).values_list('item_id', flat=True):
            cleanup_intake(item_id, store)
    item_ids = tuple(due.filter(state__in=['QUEUED', 'READY']).values_list('item_id', flat=True))
    if dispatch:
        for item_id in item_ids:
            dispatch(str(item_id))
    return item_ids
