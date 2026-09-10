"""Fresh source identities, independent of inheritable excerpt review tokens."""

from dataclasses import dataclass, field
from copy import deepcopy
import json

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.facts.extraction import section_candidates
from apps.facts.models import Fact, FactExtraction, FactRevision
from apps.facts.readmodels import digest, effective_fact, fact_queryset
from apps.processing.models import ParsingVersion
from apps.processing.value_objects import InvalidRegion, normalized_polygon


SOURCE_VERSION = 'reported-cancer-sources-2'
RELEVANT_CATEGORIES = {'DIAGNOSIS', 'PATHOLOGY'}


def json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def author_state(identity):
    current = get_user_model().objects.filter(pk=identity).values('is_active').first() if identity else None
    return {'id': str(identity) if identity else None, 'active': bool(current and current['is_active'])}


def _fragments(positions):
    output = []
    for position in positions:
        if position is None:
            continue
        block, offset = position
        if output and output[-1]['block_id'] == str(block.pk) and not block.text[output[-1]['end']:offset].strip():
            output[-1]['end'] = offset + 1
            output[-1]['raw'] = block.text[output[-1]['start']:offset + 1]
        else:
            output.append({'block_id': str(block.pk), 'page_id': str(block.document_page_id),
                           'reading_order': block.reading_order, 'start': offset, 'end': offset + 1,
                           'raw': block.text[offset:offset + 1], 'polygon': deepcopy(block.polygon),
                           'confidence': str(block.confidence)})
    return tuple(output)


def _page_fragment(fact, text):
    return ({'block_id': None, 'page_id': str(fact.document_page_id), 'reading_order': None,
             'start': None, 'end': None, 'raw': text, 'polygon': None, 'confidence': None},)


def _excerpt_positions(fact, blocks, document_type):
    """Reproduce the actual section identity, then bind a unique original view.

    Only whitespace is skipped. No normalization changes source characters.
    Repeated full sections inside one provider block are unresolved rather than
    bound to the first text occurrence. Existing Fact/evidence rows are untouched.
    """
    sections = section_candidates(blocks, document_type)
    if fact.reading_order >= len(sections):
        return ()
    section = sections[fact.reading_order]
    if (section['page'] != fact.document_page_id or section['category'] != fact.category
            or '\n'.join(section['lines']) != fact.raw_text):
        return ()
    text, original = [], []
    for block in section['blocks']:
        if block.document_page_id != fact.document_page_id or block.parsing_version_id != fact.parsing_version_id:
            return ()
        try:
            normalized_polygon(block.polygon)
        except (InvalidRegion, TypeError):
            return ()
        for offset, char in enumerate(block.text):
            if not char.isspace():
                text.append(char)
                original.append((block, offset))
    target = ''.join(char for char in fact.raw_text if not char.isspace())
    view = ''.join(text)
    found = view.find(target)
    if not target or found < 0 or view.find(target, found + 1) >= 0:
        return ()
    iterator = iter(original[found:found + len(target)])
    return tuple(None if char.isspace() else next(iterator) for char in fact.raw_text)


@dataclass(frozen=True)
class FactInput:
    fact: object
    text: str
    category: str
    input_snapshot: dict
    input_fingerprint: str
    source_token: str
    source_valid: bool
    status: str
    fragments: tuple = ()
    positions: tuple = ()
    confidence_values: tuple = ()
    binding_kind: str = 'UNVERIFIED'
    reason: str = 'source_unverified'
    display_context: dict = field(default_factory=dict)
    requires_review: bool = False

    def candidates(self):
        from .matching import literal_candidates, typed_histology_candidates
        if self.fact.representation == 'FIELD':
            from .typed_sources import candidates
            return candidates(self, typed_histology_candidates)
        return literal_candidates(self.text, self.category)

    def candidate_binding(self, row):
        if (self.text[row['start']:row['end']] != row['raw']
                or self.text[row['match_start']:row['match_end']] != row['label_raw']):
            raise ValueError('候选区间与当前摘录不符。')
        if self.binding_kind == 'OCR':
            fragments = _fragments(self.positions[row['start']:row['end']])
            labels = _fragments(self.positions[row['match_start']:row['match_end']])
        else:
            fragments = _page_fragment(self.fact, row['raw'])
            labels = _page_fragment(self.fact, row['label_raw'])
        return {'fragments': list(fragments), 'label_fragments': list(labels),
                'section_fragments': list(self.fragments), 'binding_kind': self.binding_kind,
                'input_fingerprint': self.input_fingerprint,
                'document_lifecycle': self.fact.document.lifecycle_revision,
                'source_token': self.source_token}


@dataclass(frozen=True)
class SourceScope:
    key: str
    document: object
    parsing_version: object
    source_fact: object
    input_snapshot: dict
    input_fingerprint: str
    facts: tuple
    complete: bool
    reason: str = ''
    live_fingerprint: str = ''
    narratives: tuple = ()
    preferred_narrative_keys: frozenset = frozenset()


class SourceContext:
    """One read only; create a new context for every post-render validation."""

    def __init__(self):
        self._facts = {}
        self._versions = {}
        self._narratives = {}

    def narratives(self, identity):
        from .narrative_sources import capture
        key = str(getattr(identity, 'pk', identity))
        if key not in self._narratives:
            version, blocks, _ = self.version(key)
            self._narratives[key] = capture(self, version, blocks)
        return self._narratives[key]

    def narrative(self, candidate):
        from .narrative_sources import current_input
        return current_input(self, candidate)

    def version(self, identity):
        key = str(getattr(identity, 'pk', identity))
        if key not in self._versions:
            version = ParsingVersion.objects.select_related('document__patient__account', 'document_summary').get(pk=key)
            blocks = list(version.ocr_blocks.select_related('document_page').order_by('document_page__page_number', 'reading_order', 'pk'))
            summary = getattr(version, 'document_summary', None)
            snapshot = {'id': key, 'document_id': str(version.document_id), 'parser_version': version.parser_version,
                        'ocr_provider': version.ocr_provider, 'ocr_provider_version': version.ocr_provider_version,
                        'dictionary_version': version.dictionary_version, 'dictionary_hash': version.dictionary_hash,
                        'summary': json_value({field.name: getattr(summary, field.name) for field in summary._meta.fields}) if summary else None,
                        'blocks': [{'id': str(block.pk), 'page': str(block.document_page_id), 'order': block.reading_order,
                                    'text': block.text, 'polygon': block.polygon, 'layout_polygon': block.layout_polygon,
                                    'confidence': str(block.confidence)} for block in blocks]}
            # Publication/activation fields intentionally live outside extraction
            # input identity. A successful READY receipt survives normal publish.
            self._versions[key] = (version, blocks, snapshot)
        return self._versions[key]

    @staticmethod
    def document_input(document):
        return {'id': str(document.pk), 'patient_id': str(document.patient_id), 'sha256': document.sha256,
                'lifecycle_revision': document.lifecycle_revision, 'author': author_state(document.created_by_id)}

    @staticmethod
    def document_live(document):
        return {'input': SourceContext.document_input(document), 'deleted_at': document.deleted_at,
                'purged_at': document.purged_at, 'patient_deleted_at': document.patient.deleted_at,
                'owner': author_state(document.patient.account_id),
                'active_versions': list(document.parsing_versions.filter(active=True).order_by('pk').values_list('pk', 'published_at'))}

    @staticmethod
    def version_live(version):
        return {'id': str(version.pk), 'status': version.status, 'active': version.active,
                'published_at': version.published_at, 'previous_version_id': version.previous_version_id}

    @staticmethod
    def _revisions(fact):
        return [{'id': str(row.pk), 'sequence': row.sequence, 'action': row.action,
                 'author': author_state(row.author_id), 'checked_original': row.checked_original,
                 'before': row.before, 'after': row.after, 'source': row.source}
                for row in fact.revisions.order_by('sequence')]

    def fact(self, identity):
        key = str(getattr(identity, 'pk', identity))
        if key in self._facts:
            return self._facts[key]
        fact = fact_queryset().get(pk=key)
        if fact.representation != 'EXCERPT':
            from .typed_sources import capture_field
            self._facts[key] = capture_field(self, fact)
            return self._facts[key]
        row = effective_fact(fact)
        revision = FactRevision.objects.filter(pk=row['revision_id']).first() if row['revision_id'] else None
        status = revision.after['status'] if revision else 'PENDING'
        version_id = fact.parsing_version_id if fact.origin == 'AUTOMATIC' else fact.document.parsing_versions.filter(active=True).values_list('pk', flat=True).first()
        version, blocks, version_input = self.version(version_id) if version_id else (None, [], None)
        evidence = fact.evidence
        snapshot = json_value({'contract': SOURCE_VERSION, 'id': key, 'origin': fact.origin,
            'document': self.document_input(fact.document), 'page_id': str(fact.document_page_id),
            'page_number': fact.document_page.page_number, 'category': fact.category, 'raw_text': fact.raw_text,
            'automatic_content': fact.automatic_content, 'effective_content': row['content'],
            'reading_order': fact.reading_order, 'revision_number': fact.revision_number,
            'author': author_state(fact.created_by_id), 'revisions': self._revisions(fact),
            'inherited': [{'id': str(ancestor.pk), 'raw_text': ancestor.raw_text,
                           'revision_number': ancestor.revision_number, 'author': author_state(ancestor.created_by_id),
                           'revisions': self._revisions(ancestor)}
                          for ancestor in fact_queryset().filter(pk__in=row['inherited_from']).order_by('pk')],
            'evidence': {'id': str(evidence.pk), 'version_id': str(evidence.parsing_version_id),
                         'page_id': str(evidence.document_page_id), 'text': evidence.source_text,
                         'polygon': evidence.polygon, 'confidence': evidence.confidence,
                         'block_id': str(evidence.ocr_block_id) if evidence.ocr_block_id else None} if evidence else None,
            'version_input': digest(version_input) if version_input else None})
        input_fingerprint = digest(snapshot)
        live = {'input': input_fingerprint, 'document': self.document_live(fact.document),
                'version': self.version_live(version) if version else None}
        valid = (row['source_valid'] and fact.document.patient.deleted_at is None
                 and status not in {'EXCLUDED', 'DEFERRED'}
                 and (fact.origin != 'MANUAL' or author_state(fact.created_by_id)['active'])
                 and (revision is None or author_state(revision.author_id)['active'])
                 and (fact.origin != 'AUTOMATIC' or (version and version.active and version.status == 'PUBLISHED')))
        text, category = row['content']['text'], row['content']['category']
        positions = ()
        transcribed = fact.origin == 'MANUAL' or text != fact.raw_text
        if not transcribed and version:
            summary = getattr(version, 'document_summary', None)
            positions = _excerpt_positions(fact, blocks, summary.document_type if summary else 'UNKNOWN')
        kind = 'OCR' if positions else 'TRANSCRIBED' if transcribed else 'PAGE_ONLY'
        fragments = _fragments(positions) if positions else _page_fragment(fact, text)
        confidence = tuple(item['confidence'] for item in fragments) if positions else ()
        if positions and evidence:
            confidence += (str(evidence.confidence) if evidence.confidence is not None else None,)
        requires_review = False
        if fact.category in RELEVANT_CATEGORIES:
            from .typed_sources import excerpt_dependencies
            dependencies = excerpt_dependencies(self, fact, positions)
            if dependencies:
                snapshot['typed_dependencies'] = [
                    {'input': parent.input_snapshot, 'position_resolved': located}
                    for parent, located in dependencies]
                input_fingerprint = digest(snapshot)
                live.update(input=input_fingerprint, typed_dependencies=[parent.source_token for parent, _ in dependencies])
                valid = valid and all(parent.source_valid and located and parent.binding_kind == 'OCR'
                                      for parent, located in dependencies)
                confidence += tuple(value for parent, _ in dependencies for value in parent.confidence_values)
                requires_review = any(parent.requires_review for parent, _ in dependencies)
        value = FactInput(fact, text, category, snapshot, input_fingerprint, digest(live), bool(valid), status,
                          fragments, positions, confidence, kind,
                          '' if valid and kind == 'OCR' else 'source_unavailable' if not valid else 'original_review_required',
                          requires_review=requires_review)
        self._facts[key] = value
        return value

    def scopes(self, patient, *, version=None):
        patient_id = getattr(patient, 'pk', patient)
        versions = ParsingVersion.objects.filter(document__patient_id=patient_id, document__deleted_at__isnull=True)
        versions = versions.filter(pk=getattr(version, 'pk', version)) if version is not None else versions.filter(active=True)
        output = []
        for identity in versions.order_by('pk').values_list('pk', flat=True):
            current, _, version_input = self.version(identity)
            facts = tuple(self.fact(fact.pk) for fact in Fact.objects.filter(
                Q(representation='EXCERPT') | Q(representation='FIELD', field_key='specimen.histology'),
                parsing_version=current, origin='AUTOMATIC').order_by('reading_order', 'pk'))
            relevant = tuple(item for item in facts if item.category in RELEVANT_CATEGORIES or item.fact.category in RELEVANT_CATEGORIES)
            narratives = self.narratives(current.pk)
            summary = getattr(current, 'document_summary', None)
            if not relevant and not narratives.relevant and (not summary or summary.document_type not in {'DISCHARGE', 'PATHOLOGY'}):
                continue
            extraction = FactExtraction.objects.filter(parsing_version=current).first()
            facts_complete = bool(extraction and extraction.status in {'EXTRACTED', 'NO_CANDIDATES'} and current.status in {'READY', 'PUBLISHED'})
            from .typed_sources import extraction_inventory
            typed_inventory, typed_complete = extraction_inventory(current, facts, summary)
            complete = facts_complete and typed_complete and (narratives.complete or not narratives.relevant)
            key = 'version:' + str(current.pk)
            snapshot = json_value({'contract': SOURCE_VERSION, 'key': key, 'document': self.document_input(current.document),
                                  'version': version_input, 'facts': [item.input_snapshot for item in relevant],
                                  'narratives': narratives.input_snapshot,
                                  'typed_histology': typed_inventory,
                                  'extraction': {'status': extraction.status, 'rule': extraction.extractor_version,
                                                 'count': extraction.candidate_count, 'reason': extraction.reason} if extraction else None})
            output.append(SourceScope(key, current.document, current, None, snapshot, digest(snapshot), relevant,
                complete, '' if complete else 'fact_extraction_incomplete' if not facts_complete else 'typed_extraction_incomplete' if not typed_complete else 'narrative_scope_incomplete',
                digest({'scope': snapshot, 'document': self.document_live(current.document), 'version': self.version_live(current)}),
                narratives.candidates, narratives.preferred_narrative_keys))
        if version is None:
            for fact in Fact.objects.filter(
                    Q(representation='EXCERPT') | Q(representation='FIELD', field_key='specimen.histology'),
                    document__patient_id=patient_id, document__deleted_at__isnull=True, origin='MANUAL').order_by('pk'):
                source = self.fact(fact.pk)
                if source.category not in RELEVANT_CATEGORIES and fact.category not in RELEVANT_CATEGORIES:
                    continue
                key = 'manual:' + str(fact.pk)
                snapshot = {'contract': SOURCE_VERSION, 'key': key, 'fact': source.input_snapshot}
                output.append(SourceScope(key, source.fact.document, None, source.fact, snapshot, digest(snapshot),
                                          (source,), True, '', source.source_token))
        return tuple(sorted(output, key=lambda scope: scope.key))
