"""Current scope proof and parent dependency; no mutation during reads."""
from copy import deepcopy
import unicodedata

from django.core.exceptions import ValidationError

from .laterality_models import LateralityScopeBinding, LateralityScopeRange
from .laterality_schema import SCOPED_KEY, SIDE_KEYS, member_identity
from .readmodels import digest


SCOPE_RULE = 'literal_named_member_scope_v1'


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).split())


def _authors(record, *, creator_required, excluding_revision=None):
    creator = record.created_by
    history = list(record.revisions.exclude(pk=excluding_revision).select_related('author').order_by('sequence'))
    valid = (not creator_required or creator is not None) and (creator is None or creator.is_active)
    valid = valid and all(revision.author_id and revision.author.is_active for revision in history)
    return {'valid': bool(valid), 'creator': str(record.created_by_id),
            'creator_active': creator.is_active if creator else None,
            'history': [{'id': str(revision.pk), 'sequence': revision.sequence, 'author': str(revision.author_id),
                         'active': revision.author.is_active if revision.author else None,
                         'before': revision.before, 'after': revision.after} for revision in history]}


def parent_dependency(parent):
    from .clinical_readmodels import effective_field, report_state

    # A site cannot itself be a side dependency. Check before recursive reads,
    # including when malformed foreign keys were injected outside services.
    if parent is None or parent.field_key != 'lesion.site' or parent.representation != 'FIELD':
        return {'valid': False, 'usable': False, 'identity': str(parent.pk) if parent else None}
    row = effective_field(parent)
    report = parent.clinical_report
    siblings = [effective_field(field) for field in report.fields.filter(field_key='lesion.site', entity_key=parent.entity_key).order_by('pk')]
    conflicting = any(other['usable'] and other['content']['value'] != row['content']['value'] for other in siblings if other['id'] != row['id'])
    authors = _authors(parent, creator_required=parent.origin == 'MANUAL')
    report_authors = _authors(report, creator_required=report.origin == 'MANUAL')
    keys = ('id', 'content', 'status', 'revision_number', 'revision_id', 'current_source_token', 'source_valid', 'usable')
    valid = row['source_valid'] and authors['valid'] and report_authors['valid']
    return {'valid': bool(valid), 'usable': bool(valid and row['usable'] and not conflicting),
            'field': {key: deepcopy(row[key]) for key in keys}, 'authors': authors,
            'report': report_state(report), 'report_authors': report_authors,
            'siblings': [{key: deepcopy(other[key]) for key in keys} for other in siblings], 'conflict': conflicting}


def _binding(fact):
    return LateralityScopeBinding.objects.filter(fact=fact).select_related(
        'parent_site__created_by', 'parent_site__clinical_report__created_by', 'created_by',
    ).first()


def scope_material(fact):
    if fact.field_key not in SIDE_KEYS:
        return None
    binding = _binding(fact)
    if binding is None:
        return {'scope_state': 'INVALID' if fact.field_key == SCOPED_KEY else 'UNKNOWN_SCOPE',
                'binding_id': None, 'parent_id': None, 'members': [], 'valid': fact.field_key != SCOPED_KEY,
                'parent_usable': False, 'token_material': None,
                'reason': '缺少部位范围来源证明' if fact.field_key == SCOPED_KEY else '原侧别范围未记录；不能据此判断整体同侧或异侧'}
    dependency = parent_dependency(binding.parent_site)
    ranges = list(binding.ranges.select_related(
        'parent_fragment__fact__clinical_report', 'parent_fragment__ocr_block', 'parent_fragment__document_page',
        'child_fragment__fact__clinical_report', 'child_fragment__ocr_block', 'child_fragment__document_page',
    ).order_by('ordinal'))
    latest = fact.revisions.order_by('-sequence').first()
    current = latest.after['content']['value'] if latest else fact.automatic_content['value']
    valid = bool(dependency['valid'] and ranges)
    try:
        binding.clean()
        for source in ranges:
            source.clean()
        if {source.member_key for source in ranges} != {member['member_key'] for member in binding.members}:
            raise ValidationError('成员的来源区间不完整。')
        if fact.field_key == SCOPED_KEY:
            if member_identity(current) != member_identity({'members': binding.members}):
                raise ValidationError('部位成员变化须建立替代范围。')
            if any(normalized(member['site_text']) not in normalized(dependency['field']['content']['value']['text']) for member in binding.members):
                raise ValidationError('父位置已不包含所记录部位。')
        elif normalized(dependency['field']['content']['value']['text']) != normalized(binding.parent_snapshot['field']['content']['value']['text']):
            raise ValidationError('整体部位变化须重新核对范围。')
        if binding.origin == 'AUTOMATIC':
            for member in binding.members:
                raw = '\n'.join(source.raw_text for source in ranges if source.member_key == member['member_key'])
                if raw != member['raw']:
                    raise ValidationError('部位原句与不可变区间不一致。')
    except (ValidationError, KeyError, AttributeError, TypeError, ValueError):
        valid = False
    author_valid = _authors(fact, creator_required=fact.origin == 'MANUAL')['valid']
    author_valid = author_valid and (binding.origin == 'AUTOMATIC' or bool(binding.created_by_id and binding.created_by.is_active))
    valid = valid and author_valid
    range_values = [{**{key: getattr(source, key) for key in ('member_key', 'ordinal', 'source_kind', 'start_offset', 'end_offset', 'reading_order', 'raw_text', 'polygon')},
                     'id': source.pk, 'child_fragment': source.child_fragment_id, 'parent_fragment': source.parent_fragment_id,
                     'original_child_fragment': source.original_child_fragment_id, 'original_parent_fragment': source.original_parent_fragment_id,
                     'page': str(source.page_id_at_creation), 'block': str(source.block_id_at_creation)} for source in ranges]
    # A new child review does not change its own source token. Missing/inactive
    # historical authors are nevertheless checked on every read and mutation.
    material = {'binding': str(binding.pk), 'parent': str(binding.parent_site_id), 'original_parent': str(binding.original_parent_id),
                'scope': binding.scope_kind, 'rule': binding.rule_version, 'members': binding.members,
                'creator': str(binding.created_by_id), 'original_creator': str(binding.original_created_by_id),
                'original_parent_snapshot': binding.parent_snapshot, 'parent_dependency': dependency, 'ranges': range_values,
                'author_valid': bool(author_valid), 'valid': bool(valid)}
    members = deepcopy(current['members']) if fact.field_key == SCOPED_KEY else []
    return {'scope_state': binding.scope_kind if valid else 'INVALID', 'binding_id': str(binding.pk),
            'parent_id': str(binding.original_parent_id), 'members': members, 'valid': bool(valid),
            'parent_usable': dependency['usable'], 'token_material': material,
            'reason': '部位或范围来源已变化，请重新核对' if not valid else '' if dependency['usable'] else '请先核对所关联的位置字段'}


def scope_source_token(base, fact):
    scope = scope_material(fact)
    if scope is None or scope['token_material'] is None:
        return base
    return digest({'field': base, 'laterality_scope': scope['token_material']})


def apply_scope(row, fact):
    scope = scope_material(fact)
    if scope is None:
        return row
    row['laterality_scope'] = {key: deepcopy(value) for key, value in scope.items() if key != 'token_material'}
    if scope['scope_state'] == 'UNKNOWN_SCOPE':
        return row  # Preserve the historical scalar and its review state.
    latest = fact.revisions.order_by('-sequence').first()
    if latest and latest.after['status'] == 'EXCLUDED':
        row.update(status='EXCLUDED', status_label='已排除', usable=False)
    if not scope['valid'] or not scope['parent_usable']:
        row['usable'] = False
        row['source_valid'] = row['source_valid'] and scope['valid']
        if row['status'] != 'EXCLUDED':
            row.update(status='PENDING', status_label='待核对', reason=scope['reason'])
    return row


def validate_scope_review(fact, before, action, changes):
    from .revisions import FactConflict
    scope = before.get('laterality_scope')
    if not scope or scope['scope_state'] == 'UNKNOWN_SCOPE':
        return
    if action in {'CONFIRM', 'CORRECT'} and (not scope['valid'] or not scope['parent_usable']):
        raise FactConflict('请先对照原件核对当前父位置和侧别范围。')
    if action == 'CORRECT' and fact.field_key == SCOPED_KEY and isinstance(changes, dict):
        from .laterality_schema import validate_scoped_value
        validate_scoped_value(changes.get('value'))
        if member_identity(changes['value']) != member_identity(before['content']['value']):
            raise ValidationError('新增、移除、重排或改名部位须整体替换范围，不能覆盖原关联。')


def has_report_scope(report):
    return LateralityScopeBinding.objects.filter(fact__clinical_report=report).exists()


def report_restore_guard(report, *, excluding_revision=None):
    """Raw identities after field actions, independent of parent's display state."""
    from .clinical_readmodels import field_source_base, report_source_token
    values, valid = [], True
    for fact in report.fields.select_related('created_by').order_by('pk'):
        authors = _authors(fact, creator_required=fact.origin == 'MANUAL')
        valid = valid and authors['valid']
        for fragment in fact.source_fragments.all():
            try:
                fragment.clean()
            except (ValidationError, ValueError, TypeError, AttributeError):
                valid = False
        values.append({'id': str(fact.pk), 'source': field_source_base(fact), 'authors': authors,
                       'revision': fact.revision_number})
    bindings = list(LateralityScopeBinding.objects.filter(fact__clinical_report=report).order_by('pk'))
    for binding in bindings:
        try:
            binding.clean()
            for source in binding.ranges.all():
                source.clean()
        except (ValidationError, ValueError, TypeError, AttributeError):
            valid = False
        valid = valid and (binding.origin == 'AUTOMATIC' or bool(binding.created_by_id and binding.created_by.is_active))
    report_authors = _authors(report, creator_required=report.origin == 'MANUAL', excluding_revision=excluding_revision)
    valid = valid and report_authors['valid']
    return {'valid': bool(valid), 'fingerprint': digest({'source': report_source_token(report), 'authors': report_authors,
        'fields': values, 'bindings': list(LateralityScopeBinding.objects.filter(pk__in=[row.pk for row in bindings]).order_by('pk').values()),
        'ranges': list(LateralityScopeRange.objects.filter(binding__in=bindings).order_by('binding_id', 'ordinal').values())})}


def verify_report_restore(report, revision):
    from .revisions import FactConflict
    if not has_report_scope(report):
        return
    current = report_restore_guard(report, excluding_revision=revision.pk)
    if (not revision.author_id or not revision.author.is_active or not current['valid']
            or revision.after.get('laterality_guard') != current):
        raise FactConflict('报告原来源、范围或历史作者已变化，不能恢复旧范围操作。')


def attach_report_guard(report, after):
    if has_report_scope(report):
        after['laterality_guard'] = report_restore_guard(report)


def persist_scope(fact, parent, scope):
    """Called under the report/document write lock with extractor-carried pieces."""
    binding = LateralityScopeBinding(fact=fact, parent_site=parent, original_parent_id=parent.pk,
        scope_kind=scope['scope_kind'], origin='AUTOMATIC', rule_version=SCOPE_RULE,
        members=deepcopy(scope['members']), parent_snapshot=parent_dependency(parent))
    binding.full_clean()
    binding.save()
    child_fragments, parent_fragments = list(fact.source_fragments.all()), list(parent.source_fragments.all())
    ordinal = 0
    for member, pieces in zip(scope['members'], scope['pieces']):
        for piece in pieces:
            def matching(fragments):
                matches = [fragment for fragment in fragments if fragment.ocr_block_id == piece.block.pk
                           and fragment.start_offset <= piece.start < piece.end <= fragment.end_offset]
                if len(matches) != 1:
                    raise ValidationError('侧别原范围不能唯一定位到父位置和子字段。')
                return matches[0]
            child_fragment, parent_fragment = matching(child_fragments), matching(parent_fragments)
            source = LateralityScopeRange(binding=binding, member_key=member['member_key'], ordinal=ordinal,
                child_fragment=child_fragment, parent_fragment=parent_fragment,
                original_child_fragment_id=child_fragment.pk, original_parent_fragment_id=parent_fragment.pk,
                page_id_at_creation=piece.block.document_page_id, block_id_at_creation=piece.block.pk,
                source_kind='OCR', start_offset=piece.start, end_offset=piece.end,
                reading_order=piece.block.reading_order, raw_text=piece.text, polygon=piece.block.polygon)
            source.full_clean()
            source.save()
            ordinal += 1
    return binding
