"""Durable scans freeze inputs and publish only under current actor authority."""

from collections import Counter
from contextlib import closing
from datetime import timedelta
from functools import partial
import hashlib
import io
import logging
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.documents.locking import lock_document_aggregate
from apps.documents.previews import render_page
from apps.facts.clinical_readmodels import report_queryset, report_state
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient
from apps.patients.models import Patient

from .decoding import DECODER_VERSION, RULES_VERSION, decode_page, text_candidates
from .models import CloudImagingEvidence, CloudImagingScan, CloudImagingSource
from .readmodels import document_input, locked_document, page_fingerprint
from .services import CloudConflict, _token, append_revision, operation_uuid
from .url_policy import validate_url


logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=20)
MAX_SCAN_BYTES = 100 * 1024 * 1024
MAX_PAGE_CANDIDATES = 64
TERMINAL = {'SUCCEEDED', 'FAILED', 'INVALIDATED'}


@sensitive_variables()
def _current(scan, document):
    if document.deleted_at or document.purged_at:
        return None, 'document_unavailable'
    try:
        access = authorize_patient(scan.patient_id, scan.requested_by_id, 'write')
    except PermissionDenied:
        return None, 'access_changed'
    if access.membership.revision != scan.access_revision:
        return None, 'access_changed'
    if (scan.rules_version != RULES_VERSION or scan.decoder_version != DECODER_VERSION
            or scan.input_fingerprint != digest(document_input(document))):
        return None, 'source_changed'
    return access, ''


def _finish(scan, status, code='', *, now=None, summary=None, page_results=None):
    if scan.status in TERMINAL:
        return
    scan.status, scan.error_code = status, code
    scan.lease_token = scan.lease_expires_at = None
    scan.finished_at = now or timezone.now()
    if summary is None and status != 'SUCCEEDED':
        # A lost lease or rejected publication is not a negative QR finding.
        summary = {'processed_pages': 0, 'failed_pages': 0, 'unknown_pages': scan.document.page_count,
                   'no_qr_pages': 0, 'candidate_count': 0}
        page_results = [{'page_id': str(page.pk), 'page': page.page_number, 'qr_status': 'UNKNOWN',
                         'unknown': True, 'qr_reason_code': code, 'url_candidates': 0,
                         'unsupported': 0, 'undecoded': 0, 'errors': []}
                        for page in scan.document.pages.order_by('page_number')]
    if summary is not None:
        scan.summary = summary
    if page_results is not None:
        scan.page_results = page_results
    scan.save()
    result = 'succeeded' if status == 'SUCCEEDED' else 'denied' if status == 'INVALIDATED' else 'failed'
    record_audit_event(scan.requested_by_id or 'system', 'cloud_scan_completed', scan.pk, result, code or None,
                       patient_id=scan.patient_id, resource_type='cloud_scan')


@sensitive_variables()
def _locked_scan(scan_id):
    identity = CloudImagingScan.objects.filter(pk=scan_id).values('patient_id', 'document_id').first()
    if identity is None:
        return None, None
    patient = Patient.objects.select_for_update(of=('self',), no_key=True).filter(pk=identity['patient_id']).first()
    if patient is None:
        return None, None
    document, _ = lock_document_aggregate(identity['document_id'], patient_id=patient.pk)
    if document is None:
        return None, None
    document.patient = patient
    scan = CloudImagingScan.objects.select_for_update().filter(pk=scan_id).first()
    return scan, document


@sensitive_variables()
def request_scan(patient, *, actor, document_id, expected_source, operation_id, dispatch=None):
    if dispatch is None:
        from .tasks import safe_enqueue_scan
        dispatch = safe_enqueue_scan
    with transaction.atomic():
        access = authorize_patient(patient, actor, 'write', lock=True)
        operation, expected = operation_uuid(operation_id), _token(expected_source)
        document = locked_document(access.patient, document_id)
        fingerprint = digest(document_input(document))
        prior = CloudImagingScan.objects.filter(patient=access.patient, operation_id=operation).first()
        if prior:
            if prior.document_id != document.pk or prior.input_fingerprint != expected or prior.requested_by_id != access.actor.pk:
                raise CloudConflict('此提交已处理且内容不一致，请刷新后重新提交。')
            return prior
        if fingerprint != expected:
            raise CloudConflict('扫描原件或解析已变化，请刷新后重新提交。')
        for prior in document.cloud_imaging_scans.select_for_update().filter(status__in=['QUEUED', 'RUNNING']).order_by('pk'):
            _, error = _current(prior, document)
            if error:
                _finish(prior, 'INVALIDATED', error)
            elif prior.status == 'RUNNING' and (prior.lease_expires_at is None or prior.lease_expires_at <= timezone.now()):
                _finish(prior, 'FAILED', 'lease_expired')
            else:
                return prior
        completed = document.cloud_imaging_scans.filter(
            input_fingerprint=fingerprint, rules_version=RULES_VERSION, decoder_version=DECODER_VERSION,
            status='SUCCEEDED',
        ).order_by('-created_at', '-pk').first()
        if completed:
            record_audit_event(access.actor, 'cloud_scan_requested', completed.pk, 'succeeded', 'reused_current_result',
                               patient_id=access.patient.pk, resource_type='cloud_scan', request_id=operation)
            return completed
        scan = CloudImagingScan.objects.create(patient=access.patient, document=document, requested_by=access.actor,
            access_revision=access.membership.revision, operation_id=operation, input_fingerprint=fingerprint,
            rules_version=RULES_VERSION, decoder_version=DECODER_VERSION)
        record_audit_event(access.actor, 'cloud_scan_requested', scan.pk, 'scheduled', patient_id=access.patient.pk,
                           resource_type='cloud_scan', request_id=operation)
        transaction.on_commit(partial(dispatch, scan.pk))
        return scan


@sensitive_variables()
def _original(store, document):
    if document.byte_size > MAX_SCAN_BYTES:
        raise ValueError('original_size_limit')
    output, checksum = bytearray(), hashlib.sha256()
    with closing(store.open_private(document.original_object_key)) as source:
        while True:
            part = source.read(min(256 * 1024, document.byte_size + 1 - len(output)))
            if not isinstance(part, (bytes, bytearray, memoryview)):
                raise ValueError('original_integrity_failed')
            if not part:
                break
            output.extend(part)
            checksum.update(part)
            if len(output) > document.byte_size:
                raise ValueError('original_integrity_failed')
    if len(output) != document.byte_size or checksum.hexdigest() != document.sha256:
        raise ValueError('original_integrity_failed')
    return bytes(output)


@sensitive_variables()
def _scan_pages(document, inputs, payload):
    results, candidates = [], []
    blocks = {}
    for block in inputs['blocks']:
        blocks.setdefault(str(block['document_page_id']), []).append(block)
    for page in inputs['pages']:
        page_rows, errors = [], []
        try:
            png = render_page(io.BytesIO(payload), document.content_type, page['page_number'])
            decoded = decode_page(png)
            for row in decoded['candidates']:
                if row['reason_code'] == 'payload_too_long':
                    errors.append({'code': 'payload_too_long', 'sha256': row['unavailable_payload_sha256'],
                                   'length': row['unavailable_payload_length']})
                else:
                    page_rows.append({**row, 'kind': 'QR', 'page_id': page['id']})
        except Exception:
            # Raw renderer/decoder exceptions can include input payloads.
            decoded = {'status': 'FAILED', 'failed_attempts': 0, 'reason_code': 'page_render_failed'}
        for block in blocks.get(str(page['id']), []):
            for row in text_candidates(block['text']):
                if row['reason_code'] == 'payload_too_long':
                    errors.append({'code': 'payload_too_long', 'sha256': row['unavailable_payload_sha256'],
                                   'length': row['unavailable_payload_length']})
                else:
                    page_rows.append({**row, 'kind': 'OCR', 'page_id': page['id'], 'block_id': block['id'], 'polygon': block['polygon']})
        limited = len(page_rows) > MAX_PAGE_CANDIDATES
        if limited:
            errors.append({'code': 'candidate_limit', 'count': len(page_rows) - MAX_PAGE_CANDIDATES})
        selected = page_rows[:MAX_PAGE_CANDIDATES]
        candidates.extend(selected)
        counts = Counter(row['payload_type'] for row in selected)
        results.append({'page_id': str(page['id']), 'page': page['page_number'], 'qr_status': decoded['status'],
                        'qr_reason_code': decoded.get('reason_code', ''), 'failed_attempts': decoded.get('failed_attempts', 0),
                        'url_candidates': counts['URL'], 'unsupported': counts['UNSUPPORTED'], 'undecoded': counts['UNDECODED'],
                        'unknown': bool(errors or decoded['status'] == 'FAILED' or decoded.get('failed_attempts')), 'errors': errors})
    summary = {'processed_pages': len(results), 'failed_pages': sum(row['qr_status'] == 'FAILED' for row in results),
               'unknown_pages': sum(row['unknown'] for row in results), 'no_qr_pages': sum(row['qr_status'] == 'NO_QR' and not row['unknown'] for row in results),
               'candidate_count': len(candidates)}
    return results, summary, candidates


@sensitive_variables()
def _prefill_report(document, evidence):
    candidates = []
    for report in report_queryset().filter(document=document).order_by('pk'):
        state = report_state(report)
        if not state['source_valid'] or state['status'] != 'ACTIVE':
            continue
        spans = list(report.spans.all())
        if evidence.kind == 'OCR':
            covered = any(span.ocr_block_id == evidence.ocr_block_id and span.start_offset <= evidence.start_offset
                          and span.end_offset >= evidence.end_offset for span in spans)
        else:
            # A shared footer or merely sharing a page does not prove ownership.
            covered = False
            if evidence.polygon:
                for span in spans:
                    polygon = span.ocr_block.polygon if span.ocr_block_id else None
                    if span.document_page_id == evidence.document_page_id and polygon:
                        left, right = min(p[0] for p in polygon), max(p[0] for p in polygon)
                        top, bottom = min(p[1] for p in polygon), max(p[1] for p in polygon)
                        if all(left <= x <= right and top <= y <= bottom for x, y in evidence.polygon):
                            covered = True
        if covered:
            candidates.append(report)
    return candidates[0] if len(candidates) == 1 else None


@sensitive_variables()
def _publish(access, scan, document, rows):
    active = document.parsing_versions.filter(active=True).first()
    pages = {str(page.pk): page for page in document.pages.all()}
    blocks = {str(block.pk): block for block in active.ocr_blocks.all()} if active else {}
    for index, row in enumerate(rows):
        page = pages[str(row['page_id'])]
        values = {key: row[key] for key in ('payload', 'payload_sha256', 'payload_type', 'reason_code', 'polygon')}
        if row['kind'] == 'OCR':
            values.update(ocr_block=blocks[str(row['block_id'])], start_offset=row['start_offset'], end_offset=row['end_offset'])
        else:
            values.update({key: row[key] for key in ('input_sha256', 'render_profile', 'decoder_version', 'transform')})
        evidence = CloudImagingEvidence.objects.create(document=document, document_page=page, parsing_version=active, scan=scan,
            kind=row['kind'], document_sha256=document.sha256, page_width=page.width, page_height=page.height,
            source_fingerprint=page_fingerprint(document, page), **values)
        target = validate_url(row['payload']) if row['payload_type'] == 'URL' else None
        operation = uuid.uuid5(scan.pk, f'candidate:{index}')
        source = CloudImagingSource.objects.create(patient=access.patient, document=document, evidence=evidence,
            report=_prefill_report(document, evidence), current_url=target.value if target else '', site_label=target.site_label if target else '',
            title='二维码来源' if row['kind'] == 'QR' else '原文地址', created_by=access.actor, updated_by=access.actor, operation_id=operation)
        append_revision(access, source, action='ADD', operation=operation,
                        request_hash=digest({'scan': str(scan.pk), 'candidate': index, 'payload_sha256': row['payload_sha256']}),
                        before={}, previous_token=scan.input_fingerprint)


@sensitive_variables()
def run_scan(scan_id, store):
    with transaction.atomic():
        scan, document = _locked_scan(scan_id)
        if scan is None or scan.status != 'QUEUED':
            return
        _, error = _current(scan, document)
        if error:
            _finish(scan, 'INVALIDATED', error)
            return
        scan.status, scan.lease_token = 'RUNNING', uuid.uuid4()
        scan.lease_expires_at = timezone.now() + LEASE
        scan.attempts += 1
        scan.save()
        lease, inputs = scan.lease_token, document_input(document)
    try:
        try:
            original = _original(store, document)
        except Exception:
            with transaction.atomic():
                current, current_document = _locked_scan(scan_id)
                if current and current.status == 'RUNNING' and current.lease_token == lease:
                    _, error = _current(current, current_document)
                    _finish(current, 'INVALIDATED' if error else 'FAILED', error or 'original_read_failed',
                            summary={'processed_pages': 0, 'failed_pages': 0, 'unknown_pages': document.page_count,
                                     'no_qr_pages': 0, 'candidate_count': 0})
            return
        results, summary, rows = _scan_pages(document, inputs, original)
        with transaction.atomic():
            current, document = _locked_scan(scan_id)
            if current is None or current.status != 'RUNNING' or current.lease_token != lease:
                return
            access, error = _current(current, document)
            if error:
                _finish(current, 'INVALIDATED', error)
                return
            if current.lease_expires_at is None or current.lease_expires_at <= timezone.now():
                _finish(current, 'FAILED', 'lease_expired')
                return
            _publish(access, current, document, rows)
            _finish(current, 'FAILED' if summary['unknown_pages'] else 'SUCCEEDED',
                    'pages_incomplete' if summary['unknown_pages'] else '', summary=summary, page_results=results)
    except Exception:
        logger.warning('Cloud source scan failed', extra={'scan_id': str(scan_id), 'error_code': 'scan_processing_failed'})
        with transaction.atomic():
            current, document = _locked_scan(scan_id)
            if current and current.status == 'RUNNING' and current.lease_token == lease:
                _, error = _current(current, document)
                _finish(current, 'INVALIDATED' if error else 'FAILED', error or 'scan_processing_failed')


def recover_scans(*, dispatch, now=None, limit=100):
    now = now or timezone.now()
    identities = list(CloudImagingScan.objects.filter(status__in=['QUEUED', 'RUNNING']).order_by('updated_at', 'pk').values_list('pk', flat=True)[:limit])
    for identity in identities:
        with transaction.atomic():
            scan, document = _locked_scan(identity)
            if scan is None or scan.status in TERMINAL:
                continue
            _, error = _current(scan, document)
            if error:
                _finish(scan, 'INVALIDATED', error, now=now)
            elif scan.status == 'RUNNING' and (scan.lease_expires_at is None or scan.lease_expires_at <= now):
                _finish(scan, 'FAILED', 'lease_expired', now=now)
            elif scan.status == 'QUEUED':
                transaction.on_commit(partial(dispatch, scan.pk))
            CloudImagingScan.objects.filter(pk=scan.pk).update(updated_at=now)
    return len(identities)
