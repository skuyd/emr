"""Explicit parent-bound scope replacement; no old automatic row is rewritten."""
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.exports.services import invalidate_document_exports
from apps.operations.audit import record_audit_event
from apps.patients.access import Capability, authorize_patient
from apps.processing.models import SourceEvidence

from .clinical_readmodels import field_source_base, report_state
from .clinical_schema import field_content
from .clinical_services import _report
from .laterality import _authors, normalized, parent_dependency, scope_material
from .laterality_schema import SCOPED_KEY, SIDE_KEYS
from .models import (Fact, FactSourceFragment, LateralityScopeBinding, LateralityScopeRange,
                     LateralityScopeOperation, LateralityScopeOperationRevision)
from .readmodels import digest, effective_fact, fact_queryset
from .revisions import FactConflict, revise_fact


MANUAL_RULE = 'explicit_scope_replacement_v1'


def _field(access, identity):
    field = fact_queryset().filter(pk=identity, document__patient=access.patient, representation='FIELD').first()
    if field is None:
        raise PermissionDenied
    return field


def _check(field, revision, source):
    row = effective_fact(field)
    if (type(revision) is not int or field.revision_number != revision or source != row['current_source_token']
            or row['historical']):
        raise FactConflict('字段或来源已变化，请重新打开父位置与侧别原件。')
    return row


def _locked(access, parent_id, old_id=None):
    initial = _field(access, old_id or parent_id)
    report = _report(access, initial.clinical_report_id)
    old = _field(access, old_id) if old_id else None
    parent = _field(access, parent_id)
    if (parent.field_key != 'lesion.site' or parent.clinical_report_id != report.pk
            or (old and (old.field_key not in SIDE_KEYS or old.entity_key != parent.entity_key))):
        raise ValidationError('请选择同一报告观察内的位置字段，不能跨实体替换。')
    state = report_state(report)
    if not state['source_valid'] or state['status'] != 'ACTIVE':
        raise FactConflict('原报告已排除或来源不可用，请先处理报告范围。')
    return report, parent, old


def _proof(parent, scope_kind, value, ranges):
    key = SCOPED_KEY if scope_kind == 'NAMED_MEMBERS_ONLY' else 'lesion.laterality'
    if scope_kind not in {'NAMED_MEMBERS_ONLY', 'WHOLE_ENTITY'}:
        raise ValidationError('请选择明确的侧别作用范围。')
    field_content(key, value, '范围核对')
    parent_value = effective_fact(parent)['content']['value']['text']
    if scope_kind == 'WHOLE_ENTITY':
        from .clinical_extraction import _laterality
        if _laterality(parent_value) != value['code']:
            raise ValidationError('原位置不能明确证明完整组侧别，请按列明部位核对，不能把混组整体标为双侧。')
        members = [{'member_key': 'whole', 'site_text': parent_value, 'code': value['code'], 'raw': parent_value}]
    else:
        members = deepcopy(value['members'])
    if any(normalized(member['site_text']) not in normalized(parent_value) for member in members):
        raise ValidationError('列明部位必须属于所选择的当前父位置。')
    if not isinstance(ranges, list) or not ranges or len(ranges) > 100:
        raise ValidationError('请明确每个成员的原件范围。')
    known = {member['member_key'] for member in members}
    grouped = {member['member_key']: [] for member in members}
    seen = set()
    for item in ranges:
        if not isinstance(item, dict) or item.get('member_key') not in known or type(item.get('parent_fragment_id')) is not int:
            raise ValidationError('请选择真实父位置片段及部位。')
        parent_fragment = parent.source_fragments.select_related('document_page', 'ocr_block', 'evidence').filter(pk=item['parent_fragment_id']).first()
        if parent_fragment is None:
            raise ValidationError('原区间不属于所选父位置。')
        parent_fragment.full_clean()
        if set(item) == {'member_key', 'parent_fragment_id', 'start_offset', 'end_offset'}:
            start, end = item['start_offset'], item['end_offset']
            if (parent_fragment.source_kind != 'OCR' or type(start) is not int or type(end) is not int
                    or not parent_fragment.start_offset <= start < end <= parent_fragment.end_offset):
                raise ValidationError('原字符范围超出所选父位置片段。')
            raw = parent_fragment.ocr_block.text[start:end]
            piece = {'parent': parent_fragment, 'kind': 'OCR', 'start': start, 'end': end, 'raw': raw}
            identity = ('OCR', parent_fragment.ocr_block_id, start, end)
        elif set(item) == {'member_key', 'parent_fragment_id', 'page_number', 'raw_text'}:
            if (type(item['page_number']) is not int or item['page_number'] != parent_fragment.document_page.page_number
                    or not isinstance(item['raw_text'], str) or not item['raw_text'].strip() or len(item['raw_text']) > 30000):
                raise ValidationError('人工来源须明确真实原件页及转录，不得伪造字符偏移。')
            piece = {'parent': parent_fragment, 'kind': 'MANUAL_PAGE', 'start': None, 'end': None, 'raw': item['raw_text']}
            identity = ('MANUAL_PAGE', parent_fragment.document_page_id, item['raw_text'])
        else:
            raise ValidationError('范围参数必须明确选原OCR区间或人工原件页。')
        if identity in seen:
            raise ValidationError('不能重复使用同一原件范围伪造多个成员。')
        seen.add(identity)
        grouped[item['member_key']].append(piece)
    for member in members:
        pieces = grouped[member['member_key']]
        if not pieces or normalized('\n'.join(piece['raw'] for piece in pieces)) != normalized(member['raw']):
            raise ValidationError('成员原文须与所选原区间或人工转录一致，不能借用其他部位文字。')
    return key, members, grouped


def _new_field(access, report, parent, scope_kind, value, ranges):
    key, members, grouped = _proof(parent, scope_kind, value, ranges)
    pieces = [(member['member_key'], piece) for member in members for piece in grouped[member['member_key']]]
    raw = '\n'.join(piece['raw'] for _, piece in pieces)
    content = field_content(key, value, raw)
    field = Fact(document=report.document, document_page=pieces[0][1]['parent'].document_page,
                 parsing_version=report.parsing_version, origin='MANUAL', category='IMAGING', representation='FIELD',
                 clinical_report=report, field_key=key, entity_key=parent.entity_key, schema_version=content['schema_version'],
                 raw_text=raw, automatic_content=content, reading_order=report.fields.count(), created_by=access.actor)
    field.full_clean()
    field.save()
    binding = LateralityScopeBinding(fact=field, parent_site=parent, original_parent_id=parent.pk,
        scope_kind=scope_kind, origin='MANUAL', rule_version=MANUAL_RULE, members=members,
        parent_snapshot=parent_dependency(parent), created_by=access.actor, original_created_by_id=access.actor.pk)
    binding.full_clean()
    binding.save()
    for ordinal, (member_key, piece) in enumerate(pieces):
        source = piece['parent']
        block = source.ocr_block if piece['kind'] == 'OCR' else None
        evidence = None
        if block:
            evidence = SourceEvidence.objects.create(parsing_version=report.parsing_version,
                document_page=source.document_page, ocr_block=block, polygon=block.polygon,
                source_text=piece['raw'], confidence=block.confidence)
        fragment = FactSourceFragment(fact=field, ordinal=ordinal, document_page=source.document_page,
            source_kind='OCR' if block else 'MANUAL', evidence=evidence, ocr_block=block,
            start_offset=piece['start'], end_offset=piece['end'], raw_text=piece['raw'], polygon=block.polygon if block else None)
        fragment.full_clean()
        fragment.save()
        recorded = LateralityScopeRange(binding=binding, member_key=member_key, ordinal=ordinal,
            child_fragment=fragment, parent_fragment=source, original_child_fragment_id=fragment.pk, original_parent_fragment_id=source.pk,
            page_id_at_creation=source.document_page_id, block_id_at_creation=block.pk if block else None,
            source_kind=piece['kind'], start_offset=piece['start'], end_offset=piece['end'],
            reading_order=block.reading_order if block else None, raw_text=piece['raw'], polygon=block.polygon if block else None)
        recorded.full_clean()
        recorded.save()
    return field


def _guard(fields):
    values, valid = [], True
    for identity in sorted({field.pk for field in fields if field}, key=str):
        field = fact_queryset().get(pk=identity)
        row = effective_fact(field)
        authors = _authors(field, creator_required=field.origin == 'MANUAL')
        report_authors = _authors(field.clinical_report, creator_required=field.clinical_report.origin == 'MANUAL')
        scope = scope_material(field)
        valid = valid and authors['valid'] and report_authors['valid'] and not row['historical']
        valid = valid and report_state(field.clinical_report)['source_valid'] and (scope is None or scope['valid'])
        fragments = []
        for fragment in field.source_fragments.select_related('ocr_block'):
            try:
                fragment.full_clean()
            except (ValidationError, AttributeError, TypeError, ValueError):
                valid = False
            fragments.append({'id': fragment.pk, 'block': str(fragment.ocr_block_id), 'evidence': str(fragment.evidence_id),
                              'reading_order': fragment.ocr_block.reading_order if fragment.ocr_block_id else None})
        values.append({'id': str(field.pk), 'revision': field.revision_number, 'authors': authors, 'report_authors': report_authors,
                       'source': field_source_base(field), 'scope': scope['token_material'] if scope else None, 'fragments': fragments})
    return {'valid': bool(valid), 'fingerprint': digest(values), 'fields': values}


def _revise(access, field, action):
    field.refresh_from_db()
    return revise_fact(access.patient, field.pk, actor=access.actor, action=action,
        expected_revision=field.revision_number, expected_source=effective_fact(field)['current_source_token'],
        checked_original=action == 'CONFIRM')


def _record(access, report, action, old, new, before, revisions, *, reverses=None):
    event = LateralityScopeOperation(patient=access.patient, document=report.document, action=action,
        old_fact=old, original_old_id=old.pk if old else None, new_fact=new, original_new_id=new.pk,
        author=access.actor, original_author_id=access.actor.pk, before_state=before, after_guard=_guard([old, new]),
        reverses=reverses, original_reverses_id=reverses.pk if reverses else None)
    event.full_clean()
    event.save()
    for ordinal, revision in enumerate(revisions):
        link = LateralityScopeOperationRevision(operation=event, revision=revision, original_revision_id=revision.pk, ordinal=ordinal)
        link.full_clean()
        link.save()
    invalidate_document_exports(report.document)
    record_audit_event(access.actor, 'laterality_scope_changed', event.pk, 'succeeded', action.lower())
    return event


def _replace(patient, *, actor, parent_id, expected_parent_revision, expected_parent_source, scope_kind, value, ranges,
             checked_original, confirm, fact_id=None, expected_revision=None, expected_source=None, action='REPLACE'):
    if checked_original is not True or type(confirm) is not bool:
        raise ValidationError('请实际对照原件核对部位和侧别作用范围。')
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        report, parent, old = _locked(access, parent_id, fact_id)
        _check(parent, expected_parent_revision, expected_parent_source)
        if not parent_dependency(parent)['usable']:
            raise FactConflict('请先核对当前、无冲突的父位置字段。')
        before = {'old': _check(old, expected_revision, expected_source) if old else None}
        if action == 'ATTEST':
            if old.field_key != 'lesion.laterality' or scope_material(old)['scope_state'] != 'UNKNOWN_SCOPE':
                raise ValidationError('该独立核对入口仅用于原范围未记录的侧别；已有范围请使用替换。')
            value = deepcopy(before['old']['content']['value'])
        new = _new_field(access, report, parent, scope_kind, value, ranges)
        revisions = [_revise(access, old, 'EXCLUDE')] if old else []
        if confirm:
            revisions.append(_revise(access, new, 'CONFIRM'))
        return _record(access, report, action, old, new, before, revisions)


def replace_laterality_scope(patient, **kwargs):
    return _replace(patient, action='REPLACE', **kwargs)


def add_laterality_scope(patient, **kwargs):
    return _replace(patient, action='ADD', **kwargs)


def attest_whole_laterality(patient, **kwargs):
    return _replace(patient, action='ATTEST', scope_kind='WHOLE_ENTITY', value=None, **kwargs)


def _operation(access, identity):
    event = LateralityScopeOperation.objects.select_related('author', 'old_fact', 'new_fact').filter(pk=identity, patient=access.patient).first()
    if event is None:
        raise PermissionDenied
    return event


def _operation_state(event):
    refs = list(event.revision_links.select_related('revision').order_by('ordinal'))
    valid = bool(event.author_id and event.author.is_active and event.new_fact_id == event.original_new_id
                 and event.old_fact_id == event.original_old_id and event.new_fact_id)
    valid = valid and all(link.revision_id == link.original_revision_id and link.revision_id for link in refs)
    current = _guard([event.old_fact, event.new_fact]) if event.new_fact_id else {'valid': False}
    reversed_already = LateralityScopeOperation.objects.filter(reverses=event).exists()
    material = {'guard': current, 'refs': [(str(link.pk), str(link.revision_id), str(link.original_revision_id)) for link in refs],
                'author': str(event.author_id), 'valid': bool(valid), 'reversed': reversed_already}
    return {'id': str(event.pk), 'action': event.action, 'old_id': str(event.original_old_id) if event.original_old_id else None,
            'new_id': str(event.original_new_id), 'current_token': digest(material), 'source_valid': bool(valid and current['valid']),
            'can_undo': bool(valid and current['valid'] and current == event.after_guard and not reversed_already and event.action != 'UNDO')}


def operation_material(patient, *, actor, operation_id):
    access = authorize_patient(patient, actor)
    result = _operation_state(_operation(access, operation_id))
    authorize_patient(patient, actor)
    return result


def undo_laterality_scope(patient, *, actor, operation_id, expected_operation):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        initial = _operation(access, operation_id)
        if initial.new_fact_id is None:
            raise FactConflict('替代字段已不可用，不能恢复旧确认。')
        report = _report(access, initial.new_fact.clinical_report_id)
        event = _operation(access, operation_id)
        current = _operation_state(event)
        if not current['can_undo'] or current['current_token'] != expected_operation:
            raise FactConflict('父位置、字段修订、来源或历史作者已变化，不能撤销覆盖。')
        revisions = [_revise(access, event.new_fact, 'EXCLUDE')]
        if event.old_fact_id:
            old_status = event.before_state['old']['status']
            revisions.append(_revise(access, event.old_fact, 'EXCLUDE' if old_status == 'EXCLUDED' else 'REVOKE'))
        return _record(access, report, 'UNDO', event.old_fact, event.new_fact, {'operation': str(event.pk), 'state': current}, revisions, reverses=event)
