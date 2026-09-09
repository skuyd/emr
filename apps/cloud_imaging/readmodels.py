"""Private, current source views. Callers recheck read_token after rendering."""

import hashlib
from hmac import compare_digest
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from apps.documents.locking import lock_document_aggregate
from apps.facts.clinical_readmodels import report_queryset, report_state
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .models import CloudImagingSource
from .url_policy import validate_url


SOURCE_RULE = 'cloud-source-proof-v1'


def locked_document(patient, document_id):
    document, _ = lock_document_aggregate(document_id, patient_id=patient.pk)
    if document is None or document.deleted_at or document.purged_at:
        raise PermissionDenied('资料不可用。')
    document.patient = patient
    return document


@sensitive_variables()
def document_identity(document):
    active = document.parsing_versions.filter(active=True).values(
        'id', 'status', 'published_at', 'updated_at', 'parser_version', 'ocr_provider', 'ocr_provider_version',
    ).first()
    return {
        'rule': SOURCE_RULE, 'id': str(document.pk), 'patient': str(document.patient_id),
        'sha256': document.sha256, 'content_type': document.content_type, 'byte_size': document.byte_size,
        'object_key': document.original_object_key, 'page_count': document.page_count,
        'lifecycle': document.lifecycle_revision, 'material': document.material_revision,
        'material_override': document.material_override, 'deleted_at': document.deleted_at,
        'purged_at': document.purged_at, 'active': active,
    }


@sensitive_variables()
def page_fingerprint(document, page):
    return digest({'document': document_identity(document), 'page': {
        'id': str(page.pk), 'document_id': str(page.document_id), 'number': page.page_number,
        'width': page.width, 'height': page.height,
    }})


@sensitive_variables()
def document_input(document):
    """Complete scanner/add-form input; private strings only enter its hash."""
    pages = list(document.pages.order_by('page_number', 'pk').values('id', 'page_number', 'width', 'height'))
    active = document.parsing_versions.filter(active=True).first()
    blocks = list(active.ocr_blocks.order_by('document_page_id', 'reading_order', 'pk').values(
        'id', 'document_page_id', 'reading_order', 'text', 'polygon',
    )) if active else []
    reports = [report_state(row) for row in report_queryset().filter(document=document).order_by('pk')]
    return {'document': document_identity(document), 'pages': pages, 'blocks': blocks, 'reports': reports}


def source_queryset():
    return CloudImagingSource.objects.select_related(
        'patient__account', 'document', 'evidence__document_page', 'evidence__parsing_version',
        'evidence__ocr_block', 'evidence__scan', 'report__document__patient__account', 'report__parsing_version',
        'created_by', 'updated_by',
    ).prefetch_related('revisions__author')


def _author(account):
    return str(account.pk) if account is not None and account.is_active else None


@sensitive_variables()
def source_values(source):
    # No actor identity in JSON history: actual FK can be anonymized by purge.
    return {'url': source.current_url, 'title': source.title, 'site_label': source.site_label,
            'evidence_id': str(source.evidence_id), 'report_id': str(source.report_id) if source.report_id else None,
            'status': source.status}


@sensitive_variables()
def source_material(source):
    from .decoding import DECODER_VERSION, RULES_VERSION

    evidence, document = source.evidence, source.document
    evidence.document = document
    valid = not bool(document.deleted_at or document.purged_at)
    try:
        evidence.clean()
        valid = valid and evidence.source_fingerprint == page_fingerprint(document, evidence.document_page)
    except ValidationError:
        valid = False
    scan_identity = None
    if evidence.scan_id:
        scan_identity = {'id': str(evidence.scan_id), 'rules': evidence.scan.rules_version,
                         'decoder': evidence.scan.decoder_version,
                         'current_rules': RULES_VERSION, 'current_decoder': DECODER_VERSION}
        valid = valid and evidence.scan.rules_version == RULES_VERSION and evidence.scan.decoder_version == DECODER_VERSION
    try:
        target = validate_url(source.current_url)
        target_valid = target.site_label == source.site_label and source.current_url == evidence.payload
    except ValidationError:
        target_valid = False
    report = report_state(source.report) if source.report_id else None
    if report:
        valid = valid and report['source_valid'] and report['status'] == 'ACTIVE' and report['document_id'] == str(document.pk)
        valid = valid and str(evidence.document_page_id) in {row['page_id'] for row in report['spans']}
    revisions = [{'id': str(row.pk), 'sequence': row.sequence, 'action': row.action,
                  'author': _author(row.author), 'before': row.before, 'after': row.after,
                  'source_token': row.source_token, 'created_at': row.created_at.isoformat()}
                 for row in source.revisions.all()]
    values = source_values(source)
    identity = {
        'id': str(source.pk), 'revision': source.revision_number, 'values': values,
        'page_fingerprint': page_fingerprint(document, evidence.document_page),
        'evidence': {'id': str(evidence.pk), 'payload_sha256': evidence.payload_sha256,
                     'raw_sha256': hashlib.sha256(evidence.payload.encode()).hexdigest(), 'kind': evidence.kind,
                     'frozen_fingerprint': evidence.source_fingerprint, 'input_sha256': evidence.input_sha256,
                     'polygon': evidence.polygon, 'transform': evidence.transform,
                     'render_profile': evidence.render_profile, 'decoder': evidence.decoder_version,
                     'offsets': [evidence.start_offset, evidence.end_offset], 'valid': valid, 'target_valid': target_valid},
        'report': report, 'scan': scan_identity, 'created_by': _author(source.created_by), 'updated_by': _author(source.updated_by),
        'revisions': revisions,
    }
    token = digest(identity)
    usable = bool(valid and target_valid and source.status == 'CONFIRMED' and source.confirmed_fingerprint == token)
    stale = not valid or (source.status == 'CONFIRMED' and not usable)
    return {
        'id': str(source.pk), 'document_id': str(document.pk), 'patient_id': str(source.patient_id),
        **values, 'status': 'STALE' if stale and source.status != 'EXCLUDED' else source.status,
        'source_valid': bool(valid), 'usable': usable, 'source_token': token,
        'revision_number': source.revision_number, 'history': revisions,
        'evidence': {'id': str(evidence.pk), 'kind': evidence.kind, 'page_id': str(evidence.document_page_id),
                     'page': evidence.document_page.page_number, 'payload': evidence.payload,
                     'payload_type': evidence.payload_type, 'reason_code': evidence.reason_code,
                     'polygon': evidence.polygon, 'start_offset': evidence.start_offset, 'end_offset': evidence.end_offset},
        'reason': ('来源或核对身份已变化，请对照当前原页重新补录或核对。' if stale
                   else '尚未获得符合访问规则的完整地址，请对照原页补录。' if not target_valid else ''),
    }


@sensitive_variables()
def document_snapshot(patient, *, actor, document_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, lock=True)
        document = locked_document(access.patient, document_id)
        inputs = document_input(document)
        material = {
            'document_id': str(document.pk), 'filename': document.display_filename,
            'input_token': digest(inputs), 'pages': inputs['pages'],
            'reports': [row for row in inputs['reports'] if row['source_valid'] and row['status'] == 'ACTIVE'],
            'sources': [source_material(row) for row in source_queryset().filter(document=document).order_by('created_at', 'pk')],
            'scans': list(document.cloud_imaging_scans.order_by('-created_at', '-pk').values(
                'id', 'status', 'page_results', 'summary', 'error_code', 'created_at', 'finished_at',
            )),
        }
        material['read_token'] = digest(material)
        return material


@sensitive_variables()
def source_details(patient, *, actor, source_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, lock=True)
        identity = CloudImagingSource.objects.filter(pk=source_id, patient=access.patient).values('document_id').first()
        if identity is None:
            raise PermissionDenied('来源不可用。')
        locked_document(access.patient, identity['document_id'])
        source = source_queryset().get(pk=source_id, patient=access.patient)
        material = source_material(source)
        material['read_token'] = digest(material)
        return material


@sensitive_variables()
def viewer_location(patient, *, actor, document_id, source_id, evidence_id, source_token):
    """Resolve only the current private source's original-page location."""
    try:
        source_id, evidence_id = UUID(str(source_id)), UUID(str(evidence_id))
    except (ValueError, TypeError, AttributeError):
        raise PermissionDenied('来源不可用。') from None
    row = source_details(patient, actor=actor, source_id=source_id)
    if row['document_id'] != str(document_id):
        raise PermissionDenied('来源不可用。')
    if (not row['source_valid'] or row['evidence']['id'] != str(evidence_id)
            or not isinstance(source_token, str) or not source_token.isascii()
            or not compare_digest(row['source_token'], str(source_token))):
        return None
    return {'read_token': row['read_token'], 'page': row['evidence']['page'],
            'polygon': row['evidence']['polygon']}
