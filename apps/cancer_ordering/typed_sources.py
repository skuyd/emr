"""Strict specimen.histology adapter over persisted pathology source contracts.

Immutable graph material is separate from activation and effective live tokens.
No source heading, diagnostic flag, specimen identity or OCR range is invented.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from apps.facts.clinical_context import ContextResolver
from apps.facts.clinical_readmodels import effective_field
from apps.facts.clinical_schema import validate_content
from apps.facts.models import ClinicalExtraction
from apps.facts.pathology_schema import SCHEMA
from apps.facts.pathology_source import source_material
from apps.facts.readmodels import digest

from .sources import FactInput, _fragments, _page_fragment, author_state, json_value


VERSION = 'reported-cancer-typed-histology-1'


def _record(record):
    return {field.attname: getattr(record, field.attname) for field in record._meta.fields}


def _fragment_record(fragment):
    return {'fragment': _record(fragment),
            'evidence': _record(fragment.evidence) if fragment.evidence_id else None,
            'block': _record(fragment.ocr_block) if fragment.ocr_block_id else None}


def _field_record(context, fact):
    return {'field': _record(fact), 'author': author_state(fact.created_by_id),
            'revisions': context._revisions(fact),
            'fragments': [_fragment_record(piece) for piece in fact.source_fragments.all()],
            'evidence': _record(fact.evidence) if fact.evidence_id else None}


def _authors_valid(record):
    # An inactive historical reviewer invalidates an old confirmation through
    # the full input identity, but cannot permanently prohibit a new review.
    latest = max(record.revisions.all(), key=lambda row: row.sequence, default=None)
    return ((record.origin != 'MANUAL' or author_state(record.created_by_id)['active'])
            and (latest is None or author_state(latest.author_id)['active']))


def _history_requires_review(records):
    return any(not author_state(row.author_id)['active']
               for record in records for row in record.revisions.all())


def _value_positions(roles, text):
    characters, positions = [], []
    for piece in roles['value']:
        if characters:
            characters.append('\n')
            positions.append(None)
        for offset in range(piece.start_offset, piece.end_offset):
            characters.append(piece.ocr_block.text[offset])
            positions.append((piece.ocr_block, offset))
    value = ''.join(characters)
    # The value window can contain a literal prefix or suffix; only a unique
    # exact Unicode occurrence maps to the effective value, never another block.
    start = value.find(text)
    if not text or start < 0 or value.find(text, start + 1) >= 0:
        return ()
    return tuple(positions[start:start + len(text)])


def capture_field(context, fact):
    if fact.representation != 'FIELD' or fact.field_key != 'specimen.histology':
        raise ValueError('此类型化来源仅接受组织学原文字段。')
    report = fact.clinical_report
    version_id = fact.parsing_version_id
    version, _, version_input = context.version(version_id) if version_id else (None, (), None)
    report_material = {'record': _record(report), 'author': author_state(report.created_by_id),
        'spans': [{**_record(span), 'block': _record(span.ocr_block) if span.ocr_block_id else None}
                  for span in report.spans.all()],
        'revisions': [{**_record(row), 'author': author_state(row.author_id)} for row in report.revisions.all()]}
    resolver = ContextResolver(report)
    graph = [_field_record(context, item) for _, item in sorted(resolver.fields.items())]
    latest = fact.revisions.order_by('-sequence').first()
    content = deepcopy(latest.after['content'] if latest else fact.automatic_content)
    status = latest.after['status'] if latest else 'PENDING'
    text = content.get('value', {}).get('text', '')
    if not isinstance(text, str):
        text = ''
    snapshot = json_value({'contract': VERSION, 'document': context.document_input(fact.document),
        'version_input': digest(version_input) if version_input else None,
        'field_id': str(fact.pk), 'effective_content': content, 'field_graph': graph, 'report': report_material})
    fingerprint = digest(snapshot)
    positions, roles, evaluated, row = (), None, {}, {}
    required_records = [fact, report]
    valid = False
    try:
        if fact.schema_version != SCHEMA or report.routing_kind != 'PATHOLOGY':
            raise ValidationError('来源字段模式不符。')
        validate_content(content, field_key=fact.field_key)
        fact.full_clean()
        evaluated = resolver.evaluate(fact)
        required_records += [resolver.fields[head['fact_id']]
                             for head in evaluated['snapshot']['dependency_heads']]
        row = effective_field(fact, context_resolver=resolver)
        roles = source_material(fact)
        transcribed = fact.origin == 'MANUAL' or content != fact.automatic_content
        if not transcribed and roles and roles['label']:
            positions = _value_positions(roles, text)
        valid = (row['source_valid'] and row.get('context_state') == 'RESOLVED'
            and evaluated['qualified'] and content.get('source_role') == 'CURRENT_RESULT'
            and status not in {'EXCLUDED', 'DEFERRED'} and all(_authors_valid(record) for record in required_records)
            and (fact.origin == 'MANUAL' or bool(roles and roles['label']))
            and (fact.origin != 'AUTOMATIC' or bool(version and version.active and version.status == 'PUBLISHED')))
    except (ValidationError, KeyError, ValueError, TypeError, AttributeError):
        transcribed = fact.origin == 'MANUAL' or content != fact.automatic_content
    # A historical manual correction remains transcribed after undo/revoke.
    transcribed = transcribed or any(rev.action == 'CORRECT' for rev in fact.revisions.all())
    if transcribed:
        positions = ()
    kind = 'OCR' if positions else 'TRANSCRIBED' if transcribed else 'PAGE_ONLY'
    pieces = list(fact.source_fragments.all())
    proof_points = {(str(piece.ocr_block_id), offset): (piece.ocr_block, offset)
                    for piece in pieces if piece.ocr_block_id
                    for offset in range(piece.start_offset, piece.end_offset)}
    proof_positions = sorted(proof_points.values(), key=lambda point: (
        point[0].document_page.page_number, point[0].reading_order, str(point[0].pk), point[1]))
    fragments = _fragments(proof_positions) if positions else _page_fragment(fact, text)
    confidence = tuple(str(piece.ocr_block.confidence) if piece.ocr_block.confidence is not None else None
                       for piece in pieces if piece.ocr_block_id) if positions else ()
    if positions:
        confidence += tuple(str(piece.evidence.confidence) if piece.evidence_id and piece.evidence.confidence is not None else None
                            for piece in pieces)
        confidence += tuple(str(span.ocr_block.confidence) if span.ocr_block_id and span.ocr_block.confidence is not None else None
                            for span in report.spans.all())
        # A copied anchor fragment does not replace the anchor's own evidence.
        # Confidence on every actual dependency must remain present/current.
        for head in evaluated.get('snapshot', {}).get('dependency_heads', []):
            dependency = resolver.fields[head['fact_id']]
            confidence += (str(dependency.evidence.confidence)
                           if dependency.evidence_id and dependency.evidence.confidence is not None else None,)
            for piece in dependency.source_fragments.all():
                confidence += (str(piece.ocr_block.confidence)
                               if piece.ocr_block_id and piece.ocr_block.confidence is not None else None,
                               str(piece.evidence.confidence)
                               if piece.evidence_id and piece.evidence.confidence is not None else None)
    live = {'input': fingerprint, 'document': context.document_live(fact.document),
            'version': context.version_live(version) if version else None,
            'field_context': evaluated, 'status': row.get('status'), 'valid': bool(valid)}
    display = {'source_kind': 'TYPED_HISTOLOGY', 'role': content.get('source_role'),
               'context_state': row.get('context_state', 'INVALID'),
               'report': {'id': str(report.pk), 'title': report.title}}
    # Only the validated direct SPECIMEN binding supplies private identity.
    # A same-named field elsewhere in the report is never a fallback.
    if evaluated and evaluated['state'] != 'INVALID':
        heads = {head['fact_id'] for head in evaluated['snapshot']['dependency_heads']}
        for binding in fact.automatic_content['entity_context']['bindings']:
            if binding['role'] == 'SPECIMEN' and binding['state'] == 'BOUND' and binding['target_fact_id'] in heads:
                specimen = resolver.fields[binding['target_fact_id']]
                specimen_row = effective_field(specimen, context_resolver=resolver)
                display['specimen'] = {'id': str(specimen.pk), 'label': specimen_row['content']['value']['label']}
    return FactInput(fact, text, 'PATHOLOGY', snapshot, fingerprint, digest(live), bool(valid),
                     row.get('status', status), fragments, positions, confidence, kind,
                     '' if valid and positions else 'source_unavailable' if not valid else 'original_review_required',
                     display, _history_requires_review(required_records))


def candidates(source, matcher):
    content = source.input_snapshot['effective_content']
    if source.fact.schema_version != SCHEMA:
        return ()
    try:
        validate_content(content, field_key='specimen.histology')
    except (ValidationError, KeyError, TypeError):
        return ()
    rows = matcher(source.text)
    assertion = content['value']['assertion']
    for row in rows:
        if assertion == 'UNCERTAIN':
            row['assertion'] = 'UNCERTAIN'
        elif assertion in {'NEGATIVE', 'NOT_DETECTED'}:
            row['assertion'] = 'NEGATED'
        elif assertion in {'NOT_TESTED', 'NOT_PROVIDED'}:
            row['assertion'] = 'UNKNOWN'
        # SOURCE_TEXT_ONLY and AS_REPORTED are not affirmative diagnosis flags.
        # The bounded literal matcher alone supplies local assertion/subject.
    return rows


def excerpt_dependencies(context, fact, positions):
    """Bind actual overlapping histology without replacing the legacy path.

    Unlocatable same-page fields are possible parents, never evidence of no
    overlap. Saved source ranges establish relationships; labels do not.
    """
    points = {(str(block.pk), offset) for point in positions if point is not None
              for block, offset in (point,)}
    dependencies = []
    for field in fact.document.facts.filter(representation='FIELD', field_key='specimen.histology',
            document_page_id=fact.document_page_id, parsing_version_id=fact.parsing_version_id).order_by('pk'):
        source = context.fact(field.pk)
        try:
            roles = source_material(field)
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError):
            roles = None
        value = roles['value'] if roles else ()
        ranges = {(str(piece.ocr_block_id), offset) for piece in value if piece.ocr_block_id
                  for offset in range(piece.start_offset, piece.end_offset)}
        if points and ranges and not points.intersection(ranges):
            continue
        dependencies.append((source, bool(points and ranges)))
    return tuple(dependencies)


def extraction_inventory(version, sources, summary):
    from apps.facts.clinical_extraction import EXTRACTOR_VERSION
    from apps.facts.pathology_extraction import EXTRACTOR_VERSION as PATHOLOGY_EXTRACTOR

    required = (any(source.fact.representation == 'FIELD' for source in sources)
                or bool(summary and summary.document_type == 'PATHOLOGY')
                or version.clinical_reports.filter(routing_kind='PATHOLOGY').exists())
    extraction = ClinicalExtraction.objects.filter(parsing_version=version).first()
    inventory = {'contract': VERSION, 'required': required,
                 'extraction': json_value(_record(extraction)) if extraction else None}
    complete = not required or bool(extraction and extraction.status in {'EXTRACTED', 'NO_REPORTS'}
        and extraction.extractor_version == EXTRACTOR_VERSION + '+' + PATHOLOGY_EXTRACTOR)
    return inventory, complete
