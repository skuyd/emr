"""Append-only source decisions under the same guard as access revocation."""

from copy import deepcopy
import hashlib
import re
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from apps.facts.clinical_readmodels import report_queryset, report_state
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient

from .models import CloudImagingEvidence, CloudImagingRevision, CloudImagingSource
from .readmodels import document_input, locked_document, page_fingerprint, source_details, source_material, source_queryset, source_values
from .url_policy import validate_url


class CloudConflict(ValueError):
    pass


@sensitive_variables()
def visit_source(patient, *, actor, source_id):
    """A detached notice contains no access payload or user-supplied title."""
    row = source_details(patient, actor=actor, source_id=source_id)
    if not row['usable']:
        raise CloudConflict('来源尚未确认或已变化，请重新核对当前原页。')
    target = validate_url(row['url'])
    return {'id': row['id'], 'document_id': row['document_id'], 'page': row['evidence']['page'],
            'site_label': target.site_label, 'revision_number': row['revision_number'],
            'source_token': row['source_token'], 'read_token': row['read_token']}


@sensitive_variables()
def open_source(patient, *, actor, source_id, expected_source, expected_revision):
    """Resolve a target only from a current, authorized internal resource.

    The caller reuses this check after constructing the redirect and before
    releasing it. Neither this read nor the notice creates a new confirmation.
    """
    expected = _token(expected_source)
    row = source_details(patient, actor=actor, source_id=source_id)
    if (not row['usable'] or type(expected_revision) is not int
            or row['revision_number'] != expected_revision or row['source_token'] != expected):
        raise CloudConflict('来源或核对记录已变化，请刷新后重新核对。')
    return validate_url(row['url'])


def operation_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('提交标识无效，请刷新后重试。') from None


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise CloudConflict('来源核对标识无效，请刷新后重试。')
    return value


@sensitive_variables()
def _title(value):
    if not isinstance(value, str) or len(value) > 160 or any(ord(char) < 32 for char in value):
        raise ValidationError('来源标题最多 160 个字符，不能含控制字符。')
    return value.strip()


def _page(document, page_id):
    page = document.pages.filter(pk=operation_uuid(page_id)).first()
    if page is None:
        raise ValidationError('请选择这份原件中的真实页面。')
    return page


@sensitive_variables()
def _report(document, page, report_id):
    if report_id in (None, ''):
        return None
    report = report_queryset().filter(pk=operation_uuid(report_id), document=document).first()
    if report is None:
        raise ValidationError('请选择同一原件中覆盖此页的当前报告。')
    current = report_state(report)
    if not current['source_valid'] or current['status'] != 'ACTIVE' or str(page.pk) not in {span['page_id'] for span in current['spans']}:
        raise ValidationError('请选择同一原件中覆盖此页的当前报告。')
    return report


@sensitive_variables()
def manual_evidence(document, page, url):
    active = document.parsing_versions.filter(active=True).first()
    return CloudImagingEvidence.objects.create(
        document=document, document_page=page, parsing_version=active, kind='MANUAL',
        payload=url, payload_sha256=hashlib.sha256(url.encode()).hexdigest(), payload_type='URL',
        document_sha256=document.sha256, page_width=page.width, page_height=page.height,
        source_fingerprint=page_fingerprint(document, page),
    )


@sensitive_variables()
def _prior(source, access, operation, request_hash):
    prior = source.revisions.filter(operation_id=operation).first()
    if prior and (prior.author_id != access.actor.pk or prior.request_digest != request_hash):
        raise CloudConflict('此提交已处理且内容不一致，请刷新后重新提交。')
    return prior


@sensitive_variables()
def append_revision(access, source, *, action, operation, request_hash, before, previous_token, checked_original=False):
    source.updated_by = access.actor
    source.revision_number += 1
    source.confirmed_fingerprint = ''
    source.save()
    CloudImagingRevision.objects.create(
        source=source, evidence=source.evidence, author=access.actor, sequence=source.revision_number,
        action=action, before=deepcopy(before), after=source_values(source), source_token=previous_token,
        operation_id=operation, request_digest=request_hash, checked_original=checked_original,
    )
    source = source_queryset().get(pk=source.pk)
    current = source_material(source)
    if source.status == 'CONFIRMED':
        if not current['source_valid']:
            raise CloudConflict('来源已变化，请重新核对当前原页。')
        source.confirmed_fingerprint = current['source_token']
        source.save(update_fields=['confirmed_fingerprint', 'updated_at'])
    record_audit_event(access.actor, 'cloud_source_added' if action == 'ADD' else 'cloud_source_revised',
                       source.pk, 'succeeded', action.lower(), patient_id=access.patient.pk,
                       resource_type='cloud_source', request_id=operation)
    source._prefetched_objects_cache = {}
    return source


@sensitive_variables()
def add_manual_source(patient, *, actor, document_id, page_id, url, expected_source, operation_id, title='', report_id=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, 'write', lock=True)
        operation, expected = operation_uuid(operation_id), _token(expected_source)
        document = locked_document(access.patient, document_id)
        target, title = validate_url(url), _title(title)
        request_hash = digest({'action': 'ADD', 'document': str(document.pk), 'page': str(page_id),
                               'url': target.value, 'title': title, 'report': str(report_id) if report_id else None,
                               'expected_source': expected})
        prior = CloudImagingSource.objects.filter(patient=access.patient, operation_id=operation).first()
        if prior:
            if not _prior(prior, access, operation, request_hash):
                raise CloudConflict('此提交已被其他操作使用，请刷新后重试。')
            return prior
        if digest(document_input(document)) != expected:
            raise CloudConflict('原件或可选报告已变化，请刷新后核对。')
        page = _page(document, page_id)
        report = _report(document, page, report_id)
        evidence = manual_evidence(document, page, target.value)
        source = CloudImagingSource.objects.create(
            patient=access.patient, document=document, evidence=evidence, report=report,
            current_url=target.value, site_label=target.site_label, title=title,
            created_by=access.actor, updated_by=access.actor, operation_id=operation,
        )
        return append_revision(access, source, action='ADD', operation=operation, request_hash=request_hash,
                               before={}, previous_token=expected)


@sensitive_variables()
def revise_source(patient, *, actor, source_id, action, expected_revision, expected_source,
                  operation_id, checked_original=False, changes=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, 'write', lock=True)
        operation, expected = operation_uuid(operation_id), _token(expected_source)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError('核对修订号无效。')
        identity = CloudImagingSource.objects.filter(pk=source_id, patient=access.patient).values('document_id').first()
        if identity is None:
            raise PermissionDenied('来源不可用。')
        document = locked_document(access.patient, identity['document_id'])
        # Lock only the source, never nullable joined actor/report rows.
        CloudImagingSource.objects.select_for_update().get(pk=source_id, patient=access.patient)
        source = source_queryset().get(pk=source_id, patient=access.patient)
        request_hash = digest({'action': action, 'source_id': str(source_id), 'revision': expected_revision,
                               'source_token': expected, 'checked_original': checked_original, 'changes': changes})
        if _prior(source, access, operation, request_hash):
            return source
        current = source_material(source)
        if source.revision_number != expected_revision or current['source_token'] != expected:
            raise CloudConflict('来源或核对记录已变化，请刷新后重新核对。')
        if action not in {'CONFIRM', 'CORRECT', 'REASSIGN', 'RECHECK', 'EXCLUDE', 'UNDO'}:
            raise ValidationError('请选择有效的来源核对操作。')
        if action in {'CONFIRM', 'CORRECT', 'REASSIGN', 'RECHECK'} and checked_original is not True:
            raise ValidationError('请先对照原页核对来源。')
        if changes and action not in {'CORRECT', 'REASSIGN', 'RECHECK'}:
            raise ValidationError('修改内容请使用更正或归属操作。')
        if changes is not None and (not isinstance(changes, dict) or set(changes) - {'url', 'title', 'page_id', 'report_id'}):
            raise ValidationError('来源更正字段无效。')
        changes = changes or {}
        before = source_values(source)
        if action == 'CONFIRM':
            if not current['source_valid']:
                raise CloudConflict('来源已变化，请从当前原页重新补录。')
            validate_url(source.current_url)
            source.status = 'CONFIRMED'
        elif action in {'CORRECT', 'RECHECK', 'REASSIGN'}:
            if action == 'REASSIGN' and set(changes) - {'report_id'}:
                raise ValidationError('归属操作只能修改报告归属。')
            page = _page(document, changes.get('page_id', source.evidence.document_page_id))
            target = validate_url(changes.get('url', source.current_url))
            source.report = _report(document, page, changes.get('report_id', source.report_id))
            source.title = _title(changes.get('title', source.title))
            if (action == 'RECHECK' or target.value != source.current_url or page.pk != source.evidence.document_page_id
                    or not current['source_valid']):
                # A new proof is explicitly manual; an automatic payload is never overwritten.
                source.evidence = manual_evidence(document, page, target.value)
            source.current_url, source.site_label, source.status = target.value, target.site_label, 'CONFIRMED'
        elif action == 'EXCLUDE':
            source.status = 'EXCLUDED'
        else:
            latest = source.revisions.last()
            if latest is None or not latest.before:
                raise ValidationError('没有可撤销的最近核对决定。')
            previous = latest.before
            evidence = CloudImagingEvidence.objects.filter(pk=previous['evidence_id'], document=document).first()
            if evidence is None:
                raise CloudConflict('原证据不可用，请从当前原页重新补录。')
            source.evidence = evidence
            source.current_url, source.site_label, source.title = previous['url'], previous['site_label'], previous['title']
            source.report = _report(document, evidence.document_page, previous.get('report_id'))
            source.status = 'PENDING'
        return append_revision(access, source, action=action, operation=operation, request_hash=request_hash,
                               before=before, previous_token=expected, checked_original=checked_original)
