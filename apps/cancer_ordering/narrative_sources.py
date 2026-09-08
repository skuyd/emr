"""Read-only original narrative inputs and their independently changing parents."""
from copy import deepcopy
from dataclasses import dataclass, replace

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.urls import reverse

from apps.facts.models import Fact
from apps.facts.readmodels import digest, effective_fact, fact_queryset
from apps.processing.models import OcrBlock

from . import narrative_layout, narrative_matching
from .models import CancerCandidate, NarrativeDependency, NarrativeSource
from .sources import RELEVANT_CATEGORIES, SourceContext, _excerpt_positions, _fragments, author_state, json_value


NARRATIVE_SOURCE_VERSION = 'reported-cancer-narrative-sources-1'
FRAGMENT_KEYS = ('fragments', 'label_fragments', 'heading_fragments', 'section_fragments', 'date_fragments')


def rule_version():
    return digest({'sources': NARRATIVE_SOURCE_VERSION, 'layout': narrative_layout.LAYOUT_VERSION,
                   'matching': narrative_matching.MATCHING_VERSION})


def position_key(version_id, fragments):
    """Stable original occurrence; neither parents nor their mutable values enter."""
    positions = [[item['block_id'], item['start'] + index] for item in fragments
                 for index, character in enumerate(item['raw']) if not character.isspace()]
    return digest({'version_id': str(version_id), 'positions': positions}) if positions else None


def _generation(version, page_id, role, text, data, binding, rule):
    return {'version_id': str(version.pk), 'document_id': str(version.document_id), 'page_id': str(page_id),
            'role': role, 'text': text, 'data': data, 'binding': binding, 'rule': rule}


def validate_original(source):
    if (source.document_page.document_id != source.document_id or source.parsing_version.document_id != source.document_id
            or source.role not in {'CHIEF_COMPLAINT', 'PRESENT_ILLNESS', 'AUXILIARY_FINDINGS',
                                   'ADMISSION_NARRATIVE', 'CONSULTATION_SUMMARY'}):
        raise ValidationError('叙述来源必须属于同一原件、原页及实际解析范围。')
    binding, data = source.original_source, source.original_data
    if not isinstance(binding, dict) or any(not isinstance(binding.get(key), list) for key in FRAGMENT_KEYS):
        raise ValidationError('叙述须保留完整原文和上下文的实际位置。')
    if not all(binding[key] for key in FRAGMENT_KEYS if key != 'date_fragments'):
        raise ValidationError('叙述原文、标题及字面不能缺失。')
    blocks = {str(row.pk): row for row in OcrBlock.objects.filter(parsing_version_id=source.parsing_version_id)}
    mapping = binding.get('character_map')
    if not isinstance(mapping, list) or len(mapping) != len(source.raw_text):
        raise ValidationError('叙述必须保留完整逐字符原位置。')
    positions = []
    for character, point in zip(source.raw_text, mapping):
        if point is None:
            if not character.isspace():
                raise ValidationError('非空白原文不能缺少实际位置。')
            positions.append(None)
            continue
        if not isinstance(point, list) or len(point) != 2:
            raise ValidationError('叙述原位置格式不完整。')
        block, offset = blocks.get(point[0]), point[1]
        if (block is None or str(block.document_page_id) != str(source.document_page_id) or type(offset) is not int
                or not 0 <= offset < len(block.text) or block.text[offset] != character):
            raise ValidationError('叙述字符必须来自该原页的实际位置。')
        positions.append((block, offset))
    for key in FRAGMENT_KEYS:
        for item in binding[key]:
            block = blocks.get(item.get('block_id'))
            if (block is None or str(block.document_page_id) != str(source.document_page_id)
                    or item.get('page_id') != str(block.document_page_id) or item.get('version_id') != str(block.parsing_version_id)
                    or type(item.get('start')) is not int or type(item.get('end')) is not int
                    or not 0 <= item['start'] < item['end'] <= len(block.text)
                    or block.text[item['start']:item['end']] != item.get('raw') or block.polygon != item.get('polygon')
                    or block.reading_order != item.get('reading_order')
                    or item.get('confidence') != (str(block.confidence) if block.confidence is not None else None)):
                raise ValidationError('叙述字符、几何及置信度必须等于该页原始 OCR。')
    try:
        matches = (source.raw_text[data['start']:data['end']] == data['raw']
                   and source.raw_text[data['match_start']:data['match_end']] == data['label_raw'])
        matches = matches and all(binding[key] == list(narrative_layout.source_fragments(points)) for key, points in (
            ('fragments', positions[data['start']:data['end']]),
            ('label_fragments', positions[data['match_start']:data['match_end']]),
            ('section_fragments', positions)))
        source_points = {(str(block.pk), offset) for point in positions if point is not None for block, offset in (point,)}
        matches = matches and all((item['block_id'], offset) in source_points
            for key in ('heading_fragments', 'date_fragments') for item in binding[key] for offset in range(item['start'], item['end']))
    except (KeyError, TypeError):
        matches = False
    expected = digest(_generation(source.parsing_version, source.document_page_id, source.role,
                                 source.raw_text, data, binding, source.rule_version))
    if not matches or expected != source.source_key or position_key(source.parsing_version_id, binding['label_fragments']) != source.occurrence_key:
        raise ValidationError('叙述世代与原始发生身份不一致。')


def _intersects(left, right):
    return any(a['block_id'] == b['block_id'] and max(a['start'], b['start']) < min(a['end'], b['end'])
               for a in left for b in right)


def _parent(context, fact, blocks, summary):
    """Capture all original parent types without changing the excerpt position map."""
    if fact.representation == 'EXCERPT':
        source = context.fact(fact.pk)
        positions = _excerpt_positions(fact, blocks, summary.document_type if summary else 'UNKNOWN') if fact.origin == 'AUTOMATIC' else ()
        ranges = list(_fragments(positions)) if positions else []
        snapshot = source.input_snapshot
        live, valid, status = source.source_token, source.source_valid, source.status
        corrected = source.binding_kind == 'TRANSCRIBED'
    else:
        # Structured fields retain their existing immutable source fragments and
        # actual parent report state; they never become narrative diagnoses.
        from apps.facts.clinical_readmodels import field_source_token
        state = effective_fact(fact)
        fragments = list(fact.source_fragments.select_related('evidence').order_by('ordinal'))
        ranges = [{'block_id': str(row.ocr_block_id), 'start': row.start_offset, 'end': row.end_offset,
                   'raw': row.raw_text, 'page_id': str(row.document_page_id), 'polygon': row.polygon}
                  for row in fragments if row.ocr_block_id is not None]
        report = fact.clinical_report
        report_input = {field.attname: getattr(report, field.attname) for field in report._meta.fields
                        if field.name != 'created_by'}
        report_input.update(author=author_state(report.created_by_id),
            spans=list(report.spans.order_by('ordinal').values()),
            revisions=[{'id': str(rev.pk), 'sequence': rev.sequence, 'action': rev.action,
                        'before': rev.before, 'after': rev.after, 'field_revisions': rev.field_revisions,
                        'author': author_state(rev.author_id)} for rev in report.revisions.order_by('sequence')])
        snapshot = json_value({'id': str(fact.pk), 'origin': fact.origin, 'raw_text': fact.raw_text, 'category': fact.category,
            'page_id': str(fact.document_page_id), 'document_id': str(fact.document_id), 'version_id': str(fact.parsing_version_id),
            'content': fact.automatic_content, 'revision_number': fact.revision_number, 'author': author_state(fact.created_by_id),
            'revisions': context._revisions(fact), 'fragments': [
                {'source': {field.attname: getattr(row, field.attname) for field in row._meta.fields},
                 'evidence': {field.attname: getattr(row.evidence, field.attname) for field in row.evidence._meta.fields}
                             if row.evidence_id else None} for row in fragments], 'report': report_input})
        live, valid, status = field_source_token(fact), state['source_valid'], state['status']
        latest_field, latest_report = fact.revisions.order_by('-sequence').first(), report.revisions.order_by('-sequence').first()
        valid = (valid and (fact.origin != 'MANUAL' or author_state(fact.created_by_id)['active'])
                 and (report.origin != 'MANUAL' or author_state(report.created_by_id)['active'])
                 and all(row is None or author_state(row.author_id)['active'] for row in (latest_field, latest_report)))
        corrected = state['content'] != fact.automatic_content or fact.origin == 'MANUAL'
    # A correction is not OCR evidence after revocation, even if a later action
    # returns the same text. An explicit current candidate review is required.
    history = [*snapshot.get('revisions', []), *(row for item in snapshot.get('inherited', []) for row in item['revisions'])]
    corrected = corrected or any(item['action'] == 'CORRECT' for item in history)
    return {'fact': fact, 'id': str(fact.pk), 'page_id': str(fact.document_page_id), 'ranges': ranges,
            'input': snapshot, 'live': live, 'valid': bool(valid), 'status': status, 'corrected': corrected}


@dataclass(frozen=True)
class NarrativeCandidateInput:
    document: object
    version: object
    page_id: str
    text: str
    role: str
    data: dict
    binding: dict
    occurrence_key: str
    generation_key: str
    input_snapshot: dict
    input_fingerprint: str
    source_token: str
    source_valid: bool
    status: str
    parents: tuple
    confidence_values: tuple
    requires_review: bool
    present: bool = True
    binding_kind: str = 'NARRATIVE_OCR'

    def candidate_binding(self):
        return {**deepcopy(self.binding), 'input_fingerprint': self.input_fingerprint, 'source_token': self.source_token,
                'document_lifecycle': self.document.lifecycle_revision, 'generation_key': self.generation_key}

    def source_info(self):
        page = self.document.pages.get(pk=self.page_id)
        return {'document_id': str(self.document.pk), 'filename': self.document.display_filename,
                'page': page.page_number, 'page_id': self.page_id, 'parsing_version': str(self.version.pk),
                'evidence_id': None, 'raw_text': self.text, 'polygon': None, 'location': 'PAGE',
                'url': reverse('documents:document_viewer', args=[self.document.pk]) + f'?page={page.page_number}',
                'sha256': self.document.sha256, 'role': self.role,
                'fragments': deepcopy(self.binding.get('section_fragments', []))}


@dataclass(frozen=True)
class NarrativeScope:
    candidates: tuple
    input_snapshot: dict
    complete: bool
    relevant: bool
    preferred_narrative_keys: frozenset


def capture(context, version, blocks):
    discovery = narrative_layout.discover_narratives(blocks)
    summary = getattr(version, 'document_summary', None)
    parents = tuple(_parent(context, fact, blocks, summary) for fact in fact_queryset().filter(
        Q(parsing_version=version) | Q(document=version.document, origin='MANUAL')))
    page_rows = list(version.document.pages.order_by('page_number').values('id', 'page_number', 'width', 'height', 'orientation'))
    coverage = list(discovery.coverage)
    seen = {item['page_id'] for item in coverage}
    for page in page_rows:
        if str(page['id']) not in seen:
            coverage.append({'page_id': str(page['id']), 'status': 'UNJUDGED', 'reasons': ['no_ocr'],
                             'slot_count': 0, 'relevant': False})
    coverage.sort(key=lambda item: item['page_id'])
    prior_keys = set(CancerCandidate.objects.filter(source_narrative__parsing_version=version).values_list('occurrence_key', flat=True))
    old_parents = {}
    for key, original in NarrativeDependency.objects.filter(narrative_source__parsing_version=version).values_list(
            'narrative_source__occurrence_key', 'original_fact_id'):
        old_parents.setdefault(key, set()).add(str(original))
    parent_by_id = {row['id']: row for row in parents}
    output, inputs, slots, preferred = [], [], [], set()
    version_input = context.version(version.pk)[2]
    for narrative in discovery.inputs:
        slots.append({'page_id': narrative.page_id, 'role': narrative.role, 'text': narrative.text,
            'body_start': narrative.body_start, 'heading_fragments': narrative.heading_fragments,
            'record_dates': narrative.record_dates,
            'character_map': [[str(point[0].pk), point[1]] if point is not None else None for point in narrative.positions]})
        for data in narrative_matching.narrative_candidates(narrative):
            binding = json_value(narrative.candidate_binding(data))
            binding['character_map'] = [[str(point[0].pk), point[1]] if point is not None else None for point in narrative.positions]
            key = position_key(version.pk, binding['label_fragments'])
            bound = []
            for parent in parents:
                if parent['page_id'] != narrative.page_id:
                    continue
                intersects = _intersects(binding['section_fragments'], parent['ranges']) if parent['ranges'] else True
                if intersects or parent['id'] in old_parents.get(key, set()):
                    bound.append({**parent, 'position_status': 'EXACT' if parent['ranges'] else 'UNVERIFIED'})
            for missing in sorted(old_parents.get(key, set()) - set(parent_by_id)):
                bound.append({'id': missing, 'fact': None, 'ranges': [], 'position_status': 'MISSING',
                    'input': {'id': missing, 'missing': True}, 'live': 'missing', 'valid': False,
                    'status': 'PENDING', 'corrected': False})
            bound.sort(key=lambda item: item['id'])
            legacy_overlap = any(parent['fact'] is not None and parent['fact'].category in RELEVANT_CATEGORIES
                and _intersects(binding['label_fragments'], parent['ranges']) for parent in bound)
            # A later diagnostic Fact cannot reset an already reviewed narrative
            # occurrence by supplying a clean parallel route in the next run.
            if key in prior_keys:
                preferred.add(key)
            static = _generation(version, narrative.page_id, narrative.role, narrative.text, data, binding, rule_version())
            snapshot = json_value({'source': static, 'document': context.document_input(version.document),
                'version_input': digest(version_input), 'parents': [
                    {'id': parent['id'], 'position_status': parent['position_status'], 'ranges': parent['ranges'],
                     'input': parent['input']} for parent in bound]})
            inputs.append(snapshot)
            if legacy_overlap and key not in prior_keys:
                continue
            fingerprint = digest(snapshot)
            token = digest({'input': fingerprint, 'document': context.document_live(version.document),
                            'version': context.version_live(version), 'parents': [row['live'] for row in bound]})
            valid = (version.active and version.status == 'PUBLISHED' and version.document.deleted_at is None
                     and version.document.purged_at is None and version.document.patient.deleted_at is None
                     and author_state(version.document.patient.account_id)['active']
                     and all(row['valid'] and row['status'] not in {'EXCLUDED', 'DEFERRED'} for row in bound))
            status = next((row['status'] for row in bound if row['status'] in {'EXCLUDED', 'DEFERRED'}), 'PENDING')
            output.append(NarrativeCandidateInput(version.document, version, narrative.page_id, narrative.text,
                narrative.role, data, binding, key, digest(static), snapshot, fingerprint, token, bool(valid), status,
                tuple(bound), tuple(binding['confidence_values']), any(row['corrected'] or row['position_status'] != 'EXACT' for row in bound)))
    clinical = getattr(version, 'clinical_extraction', None)
    snapshot = json_value({'rule': rule_version(), 'pages': page_rows, 'coverage': coverage, 'inputs': inputs,
                          'slots': slots, 'all_parents': [row['input'] for row in parents],
                          'clinical_extraction': {field.attname: getattr(clinical, field.attname) for field in clinical._meta.fields}
                                                 if clinical else None})
    complete = (discovery.complete and len(seen) == len(page_rows)
                and (clinical is None or clinical.status not in {'FAILED', 'PARTIAL'}))
    return NarrativeScope(tuple(output), snapshot, complete, discovery.relevant, frozenset(preferred))


def current_input(context, candidate):
    original = candidate.source_narrative
    current = next((item for item in context.narratives(original.parsing_version_id).candidates
                    if item.occurrence_key == candidate.occurrence_key), None)
    if current is not None:
        # Preserve the candidate's original generation and review lineage, but
        # validate the current immutable generation after an explicit collect.
        # A missing generation is not materialized by a read or confirmation.
        generation = NarrativeSource.objects.filter(source_key=current.generation_key).first()
        if generation is None:
            return replace(current, source_valid=False, requires_review=True)
        try:
            validate_original(generation)
        except (ValidationError, KeyError, TypeError):
            return replace(current, source_valid=False, present=False, requires_review=True)
        return current
    version, _, snapshot = context.version(original.parsing_version_id)
    token = digest({'missing': original.source_key, 'version': snapshot, 'live': context.version_live(version),
                    'document': context.document_live(version.document)})
    return NarrativeCandidateInput(version.document, version, str(original.document_page_id), original.raw_text,
        original.role, original.original_data, original.original_source, original.occurrence_key, original.source_key,
        {'missing': original.source_key}, token, token, False, 'PENDING', (), (), True, False)


def persist_source(source):
    current = NarrativeSource.objects.filter(source_key=source.generation_key).first()
    if current is None:
        current = NarrativeSource(document=source.document, document_page_id=source.page_id, parsing_version=source.version,
            source_key=source.generation_key, occurrence_key=source.occurrence_key, rule_version=rule_version(),
            role=source.role, raw_text=source.text, original_data=source.data, original_source=source.binding)
        current.full_clean()
        current.save()
    else:
        validate_original(current)
    return current
