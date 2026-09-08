"""Map actual persisted lab adapters to the neutral glucose scorer contract.

This is an offline, private-data tool, not a web API or an execution approval.
No function accepts gold, expected values or a list of reviewed source IDs.
The caller must freeze/check original and raw OCR file bytes, application,
dictionary and mapper identities before allowing any real replay.

``FixedOcrMapper`` takes unchanged cache page dictionaries, not OcrPage objects:
OcrPage sorts regions and OcrRegion strips outer whitespace. Original array
indices, reading orders, text and original-space polygons are kept separately.
Only the app's demonstrable outer-strip is allowed when linking persisted OCR;
literal field narrowing deletes whitespace for matching, never repairs letters.

``map_pipeline_document`` reads current persisted observations and invokes the
real lab_candidate for each one. Page execution states are supplied explicitly;
adapter exceptions fail evaluation without changing the recorded pipeline state.
Returned text, source receipts and diagnostics are private artifacts.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata

from apps.processing.geometry import IDENTITY_TRANSFORM, source_polygon
from apps.processing.value_objects import InvalidRegion, normalized_polygon


class MappingInputError(ValueError):
    """The trusted execution envelope cannot be established."""


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _polygon(value):
    try:
        return [list(p) for p in normalized_polygon(value)]
    except (InvalidRegion, TypeError, ValueError):
        return None


def _same_polygon(a, b):
    left, right = _polygon(a), _polygon(b)
    return bool(left and right and len(left) == len(right) and any(
        all(abs(x - y) <= 1e-6 for p, q in zip(left, right[i:] + right[:i]) for x, y in zip(p, q))
        for i in range(len(right))))


def _bounds(polygon):
    points = _polygon(polygon)
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _contains(outer, inner):
    return bool(outer and inner and outer[0] <= inner[0] + 1e-6 and outer[1] <= inner[1] + 1e-6
                and outer[2] >= inner[2] - 1e-6 and outer[3] >= inner[3] - 1e-6)


def _narrow(proofs, literal):
    """Unique literal within already located spans; return original char offsets."""
    if not isinstance(literal, str) or not literal.strip() or not proofs:
        return []
    needle = ''.join(c for c in literal if not c.isspace())
    letters, positions = [], []
    for index, proof in enumerate(proofs):
        for offset in range(proof['char_start'], proof['char_end_exclusive']):
            char = proof['block_text'][offset]
            if not char.isspace():
                letters.append(char)
                positions.append((index, offset))
    haystack = ''.join(letters)
    start = haystack.find(needle)
    if start < 0 or haystack.find(needle, start + 1) >= 0:
        return []
    result, prior = [], None
    for index, offset in positions[start:start + len(needle)]:
        if index != prior:
            result.append({**proofs[index], 'char_start': offset, 'char_end_exclusive': offset + 1})
            prior = index
        else:
            result[-1]['char_end_exclusive'] = offset + 1
    return result


class FixedOcrMapper:
    def __init__(self, fixed_pages, persisted_blocks, *, source_sha256, fixed_ocr_sha256, document_id):
        if any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x)
               for x in (source_sha256, fixed_ocr_sha256)):
            raise MappingInputError('Original and fixed OCR SHA256 are required')
        self.document_id = str(document_id)
        self.source_sha256, self.fixed_ocr_sha256 = source_sha256, fixed_ocr_sha256
        self.pages, self.blocks, self.bound, self.binding_issues, self.adjustments = {}, {}, {}, {}, []
        for page in fixed_pages:
            number = page.get('page_number')
            if type(number) is not int or number < 1 or number in self.pages:
                raise MappingInputError('Fixed pages must have unique positive page numbers')
            fixed, orders = [], set()
            for index, region in enumerate(page.get('regions', [])):
                text, order = region.get('text'), region.get('reading_order')
                if not isinstance(text, str) or type(order) is not int or order < 0:
                    raise MappingInputError('Each original region needs text and explicit reading order')
                if order in orders:
                    raise MappingInputError('Original reading order must be unique within a page')
                orders.add(order)
                polygon = source_polygon(page.get('source_transform', IDENTITY_TRANSFORM), region.get('polygon'))
                fixed.append({**self.source(number), 'region_array_index': index, 'block_reading_order': order,
                    'block_text': text, 'full_block_text_sha256': _hash(text),
                    'original_normalized_polygon': _polygon(polygon)})
            self.pages[number] = fixed
        for original in persisted_blocks:
            block = deepcopy(original)
            identity = str(block['id'])
            if identity in self.blocks:
                raise MappingInputError('Repeated persisted OCR block ID')
            self.blocks[identity] = block
            number = block.get('document_page__page_number')
            if str(block.get('parsing_version__document_id')) != self.document_id:
                self.binding_issues[identity] = 'persisted_document_mismatch'
                continue
            matches = [r for r in self.pages.get(number, [])
                       if r['block_reading_order'] == block.get('reading_order')
                       and _same_polygon(r['original_normalized_polygon'], block.get('polygon'))
                       and r['block_text'].strip() == block.get('text')]
            if len(matches) != 1:
                self.binding_issues[identity] = 'fixed_block_missing_or_ambiguous'
                continue
            fixed = matches[0]
            shift = len(fixed['block_text']) - len(fixed['block_text'].lstrip())
            self.bound[identity] = {**fixed, 'char_start': shift,
                                    'char_end_exclusive': shift + len(block['text'])}
            if fixed['block_text'] != block['text']:
                self.adjustments.append({'block_id': identity, 'rule': 'app_outer_strip', 'offset_shift': shift})

    def source(self, page):
        return {'source_sha256': self.source_sha256, 'fixed_ocr_sha256': self.fixed_ocr_sha256, 'manifest_page': page}

    def _consistent_locator(self, value, proof, block):
        for key in ('source_sha256', 'fixed_ocr_sha256', 'manifest_page', 'region_array_index', 'block_reading_order',
                    'block_text', 'full_block_text_sha256'):
            if key in value and (type(value[key]) is not type(proof[key]) or value[key] != proof[key]):
                return False
        for key, actual in [('page_id', block.get('document_page_id')), ('parsing_version_id', block.get('parsing_version_id')),
                            ('document_id', block.get('parsing_version__document_id'))]:
            if key in value and str(value[key]) != str(actual):
                return False
        if 'reading_order' in value and (type(value['reading_order']) is not int or value['reading_order'] != block['reading_order']):
            return False
        if 'original_normalized_polygon' in value and not _same_polygon(value['original_normalized_polygon'], proof['original_normalized_polygon']):
            return False
        if 'polygon' in value and not _same_polygon(value['polygon'], block.get('polygon')):
            return False
        if 'text' in value and value['text'] != block['text']:
            return False
        return True

    def spans(self, spans, *, page, version_id, issues):
        result = []
        for span in spans:
            identity = str(span.get('block_id'))
            block, proof = self.blocks.get(identity), self.bound.get(identity)
            if (not block or not proof or proof['manifest_page'] != page
                    or str(block.get('parsing_version_id')) != str(version_id)
                    or not self._consistent_locator(span, proof, block)):
                issues.append({'reason': 'context_locator_unverified', 'block_id': identity})
                return []
            start, end = span.get('start', 0), span.get('end', len(block['text']))
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block['text']):
                issues.append({'reason': 'context_offset_unverified', 'block_id': identity})
                return []
            for key, offset in [('char_start', start), ('char_end_exclusive', end)]:
                if key in span and (type(span[key]) is not int or span[key] != proof['char_start'] + offset):
                    issues.append({'reason': 'context_offset_alias_disagrees', 'block_id': identity})
                    return []
            result.append({**proof, 'char_start': proof['char_start'] + start,
                            'char_end_exclusive': proof['char_start'] + end})
        return result

    def field_blocks(self, descriptor, *, issues):
        """Resolve an actual field polygon before considering any field text."""
        evidence = descriptor.get('field_evidence', {})
        page = descriptor.get('page_number')
        polygon = evidence.get('polygon')
        if (str(descriptor.get('document_id')) != self.document_id or evidence.get('precision') != 'region'
                or evidence.get('page_number') != page or type(page) is not int or not _polygon(polygon)):
            issues.append({'reason': 'field_region_unverified'})
            return []
        candidates = [b for b in self.blocks.values() if str(b.get('parsing_version_id')) == str(descriptor.get('parsing_version_id'))
            and str(b.get('document_page_id')) == str(descriptor.get('page_id'))
            and b.get('document_page__page_number') == page and _contains(_bounds(polygon), _bounds(b.get('polygon')))]
        candidates.sort(key=lambda b: b['reading_order'])
        if not candidates:
            issues.append({'reason': 'field_region_has_no_original_blocks'})
            return []
        bounds = [_bounds(b['polygon']) for b in candidates]
        union = [min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds)]
        rectangle = [[union[0], union[1]], [union[2], union[1]], [union[2], union[3]], [union[0], union[3]]]
        if not ((len(candidates) == 1 and _same_polygon(polygon, candidates[0]['polygon'])) or _same_polygon(polygon, rectangle)):
            issues.append({'reason': 'field_region_not_exact_block_union'})
            return []
        result = []
        for block in candidates:
            proof = self.bound.get(str(block['id']))
            if not proof:
                issues.append({'reason': 'field_block_binding_unverified', 'block_id': str(block['id'])})
                return []
            # A region may describe a union, so its polygon is checked above;
            # any additional supplied locator must still identify this block.
            extra = {k: v for k, v in evidence.items() if k != 'polygon'}
            if not self._consistent_locator(extra, proof, block) or ('block_id' in extra and str(extra['block_id']) != str(block['id'])):
                issues.append({'reason': 'field_locator_disagrees'})
                return []
            if any(k in extra for k in ('start', 'end', 'char_start', 'char_end_exclusive')):
                start, end = extra.get('start', 0), extra.get('end', len(block['text']))
                if (len(candidates) != 1 or type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(block['text'])
                        or any(key in extra and (type(extra[key]) is not int or extra[key] != proof['char_start'] + offset)
                               for key, offset in [('char_start', start), ('char_end_exclusive', end)])):
                    issues.append({'reason': 'field_offset_unverified'})
                    return []
                proof = {**proof, 'char_start': proof['char_start'] + start,
                         'char_end_exclusive': proof['char_start'] + end}
            result.append(deepcopy(proof))
        return result


def _decimal(data):
    if data.get('result_type') != 'NUMERIC' or not isinstance(data.get('raw_value'), str):
        return None
    text = unicodedata.normalize('NFKC', data['raw_value']).strip()
    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', text):
        return None
    try:
        value = Decimal(text)
        return str(value) if value.is_finite() else None
    except InvalidOperation:
        return None


def _time_values(context, *, data=None):
    origin = context.get('timezone_origin', 'UNCONFIRMED')
    printed_zone = (context.get('timezone') or None) if origin == 'SOURCE_EXPLICIT' else None
    utc = context.get('utc_datetime')
    result = {'local_datetime': context.get('local') or None, 'raw': context.get('raw') or None,
        'precision': context.get('precision', 'UNKNOWN'), 'role': context.get('role'),
        'printed_timezone': printed_zone, 'printed_utc_offset': '+00:00' if printed_zone == 'UTC' else None,
        'timezone_status': origin, 'timezone_name': context.get('timezone') or None,
        'utc_offset': '+00:00' if printed_zone == 'UTC' else None, 'utc_datetime': utc}
    if data is not None:
        result.update(local_datetime=data.get('local_time') or None, precision=data.get('time_precision', 'UNKNOWN'),
            timezone_status=data.get('timezone_origin', 'UNCONFIRMED'), timezone_name=data.get('timezone') or None,
            utc_offset=data.get('utc_offset') or None, utc_datetime=data.get('measured_at'))
    return result


def map_candidate(candidate, mapper):
    """Translate an actual lab_candidate result, without deciding gold matches."""
    data, issues = candidate['data'], []
    source = data['source']
    if source.get('source_sha256') != mapper.source_sha256 or str(source.get('document_id')) != mapper.document_id:
        raise MappingInputError('Candidate and original document identity disagree')
    page = source.get('page_number')
    if type(page) is not int or page < 1:
        raise MappingInputError('Candidate has no original page envelope')
    fields, context = source.get('field_sources', {}), source.get('report_context', {})
    evidence, regions = {}, {}
    for role, field in [('item_name', 'raw_name'), ('value', 'raw_value'), ('unit', 'raw_unit')]:
        descriptor = fields.get(field, {})
        regions[role] = mapper.field_blocks(descriptor, issues=issues)
        literal = data.get(field) if field != 'raw_name' else descriptor.get('effective_value')
        evidence[role] = _narrow(regions[role], literal)
        if regions[role] and not evidence[role]:
            issues.append({'reason': 'field_literal_unverified', 'role': role})
    # The internal standard code is not a printed abbreviation. A separately
    # located code block may be retained; the name cannot stand in for the code.
    code_regions = mapper.field_blocks(fields.get('standard_code', {}), issues=issues)
    name_ids = {(p['manifest_page'], p['region_array_index']) for p in regions['item_name']}
    code_regions = [p for p in code_regions if (p['manifest_page'], p['region_array_index']) not in name_ids]
    evidence['item_code'] = code_regions if len(code_regions) == 1 else []
    anchor = fields.get('raw_value', {})
    context_page, context_version = anchor.get('page_number'), anchor.get('parsing_version_id')
    for role, key in [('sampling', 'sample_time'), ('reporting', 'report_time')]:
        clock = context.get(key, {})
        spans = mapper.spans(clock.get('evidence', []), page=context_page, version_id=context_version, issues=issues)
        evidence[role + '_label'] = _narrow(spans, clock.get('raw_label'))
        evidence[role + '_time'] = _narrow(spans, clock.get('raw'))
    specimen = mapper.spans(context.get('specimen_evidence', []), page=context_page, version_id=context_version, issues=issues)
    evidence['specimen'] = _narrow(specimen, context.get('specimen_raw'))
    heading = mapper.spans(context.get('time_slot_evidence', []), page=context_page, version_id=context_version, issues=issues)
    origin = data.get('field_origins', {}).get('time_slot')
    slot = data.get('time_slot')
    if slot == 'FASTING' and context.get('time_slot') == 'FASTING' and origin != 'LAB_REVISION':
        meal, basis, evidence['meal_context'] = 'FASTING', 'PRINTED_REQUESTED_TEST_HEADING', heading
    elif slot == 'UNSPECIFIED' and context.get('block_ids'):
        meal, basis, evidence['meal_context'] = 'NOT_STATED', 'NO_EXPLICIT_MEAL_CONDITION_ON_ORIGINAL_PANEL', []
    else:
        meal = 'UNKNOWN' if slot == 'UNSPECIFIED' else slot
        basis, evidence['meal_context'] = origin or 'UNKNOWN', []
    values = {'decimal_value': _decimal(data), 'raw_value': data.get('raw_value'),
        'unit': data.get('raw_unit'), 'raw_unit': data.get('raw_unit'),
        'source_type': {'LAB_REPORT': 'LABORATORY_REPORT'}.get(data.get('source_kind'), data.get('source_kind')),
        'specimen_raw': context.get('specimen_raw') or None,
        'sampling': _time_values(context.get('sample_time', {}), data=data),
        'reporting': _time_values(context.get('report_time', {})),
        'meal_context': {'status': meal, 'basis': basis, 'actual_preparation_independently_verified': False,
                         'clock_inference_allowed': False}}
    return {'prediction_id': str(source.get('observation_id') or ''), 'disposition': 'ADMITTED',
        'source': mapper.source(page), 'values': values, 'abstained_fields': [],
        # Row identity is the declared original field location, even after a
        # revision changes its value. Own-field value proof remains separate.
        'row_evidence': {key: deepcopy(evidence[key] or regions.get(key, [])) for key in ('value', 'item_code', 'item_name')},
        'field_evidence': evidence,
        'mapping_receipt': {'source_fingerprint': candidate.get('source_fingerprint'),
            'production_source': deepcopy(source), 'field_origins': deepcopy(data.get('field_origins', {})),
            'issues': issues, 'text_adjustments': deepcopy(mapper.adjustments)}}


def map_pipeline_document(document, fixed_pages, *, source_sha256, fixed_ocr_sha256, execution_status):
    """Read a completed isolated pipeline database; does not run OCR or mutate it.

    execution_status maps each manifest page number to COMPLETE/FAILED/NOT_RUN.
    Missing entries stay NOT_RUN. A current version is required to call the
    production adapter. Any partial outputs remain visible on failed pages.
    The caller is responsible for quiescence/identity freezing, not UI locks.
    """
    from apps.glucose.sources import GlucoseSourceUnavailable, lab_candidate
    from apps.labs.revisions import effective_observation
    from apps.processing.models import OcrBlock

    document.refresh_from_db()
    if document.sha256 != source_sha256:
        raise MappingInputError('Database document and trusted original SHA256 disagree')
    blocks = list(OcrBlock.objects.filter(parsing_version__document=document).values(
        'id', 'text', 'polygon', 'reading_order', 'document_page__page_number', 'document_page_id',
        'parsing_version_id', 'parsing_version__document_id'))
    mapper = FixedOcrMapper(fixed_pages, blocks, source_sha256=source_sha256,
                           fixed_ocr_sha256=fixed_ocr_sha256, document_id=document.pk)
    pages = {}
    for number, originals in mapper.pages.items():
        state = execution_status.get(number, 'NOT_RUN')
        if state not in ('COMPLETE', 'FAILED', 'NOT_RUN'):
            raise MappingInputError('Unknown pipeline page execution status')
        pages[number] = {'source': mapper.source(number), 'status': state, 'pipeline_status': state,
                         'items': [], 'input_blocks': deepcopy(originals), 'errors': []}
    version = document.parsing_versions.filter(active=True, status='PUBLISHED', published_at__isnull=False).first()
    result = {'schema_version': 1, 'pages': list(pages.values()), 'unattributable_items': [],
              'mapping_receipt': {'document_id': str(document.pk), 'parsing_version_id': str(version.pk) if version else None,
                  'dictionary_version': version.dictionary_version if version else None,
                  'dictionary_hash': version.dictionary_hash if version else None,
                  'block_binding_issues': deepcopy(mapper.binding_issues)}}
    if version is None:
        for page in pages.values():
            if page['status'] == 'COMPLETE':
                page['status'] = 'FAILED'
                page['errors'].append({'reason': 'no_current_published_parsing_version'})
        return result
    for observation in version.lab_observations.select_related('parsing_version__document', 'document_page', 'evidence').order_by(
            'document_page__page_number', 'reading_order', 'pk'):
        number = observation.document_page.page_number
        page = pages.get(number)
        try:
            item = map_candidate(lab_candidate(observation), mapper)
        except Exception as error:
            # Evaluation must account for each persisted observation. Unexpected
            # adapter errors are recorded as failed evaluation, never exclusion.
            normal = isinstance(error, GlucoseSourceUnavailable) and error.__cause__ is None
            diagnostic = {'error_type': type(error).__name__, 'message': str(error),
                'cause_type': type(error.__cause__).__name__ if error.__cause__ else None,
                'original_raw_value': observation.raw_value, 'original_raw_unit': observation.raw_unit}
            try:
                effective = effective_observation(observation)
                diagnostic.update(effective_raw_value=effective.raw_value, effective_raw_unit=effective.raw_unit,
                                  effective_raw_name=effective.raw_name)
            except Exception as nested:
                diagnostic['effective_read_error'] = type(nested).__name__
            descriptor = {'document_id': str(document.pk), 'parsing_version_id': str(version.pk),
                'page_id': str(observation.document_page_id), 'page_number': number,
                'field_evidence': {'page_number': number, 'polygon': observation.evidence.polygon, 'precision': 'region'}}
            problems = []
            located = mapper.field_blocks(descriptor, issues=problems)
            item = {'prediction_id': str(observation.pk), 'disposition': 'EXCLUDED' if normal else 'ABSTAINED',
                'source': mapper.source(number), 'values': {}, 'row_evidence': {}, 'field_evidence': {},
                'diagnostic_evidence': located, 'mapping_receipt': {'diagnostic': diagnostic, 'issues': problems}}
            if not normal and page:
                page['status'] = 'FAILED'
                page['errors'].append({'observation_id': str(observation.pk), 'reason': 'adapter_failure',
                                       'error_type': type(error).__name__})
        if page is None:
            result['unattributable_items'].append(item)
        else:
            page['items'].append(item)
    return result
