"""Pure, source-first scoring of a frozen glucose adapter output.

Contract version 1 (all strings/offsets address unchanged fixed OCR):

* ``predictions.pages`` is the harness's input inventory. Each page has ``source``
  (source_sha256, fixed_ocr_sha256, manifest_page), ``status`` (COMPLETE, FAILED,
  NOT_RUN), and the original ordered ``items``. Missing pages stay NOT_RUN.
* Items have ADMITTED/EXCLUDED/ABSTAINED ``disposition``, optional persisted
  ``prediction_id`` and ``source``, neutral ``values`` shaped like gold.expected,
  ``abstained_fields`` (metric IDs), ``row_evidence`` (value and item_code/name),
  and ``field_evidence`` indexed by the frozen role names. Review/pending status
  does not exclude an individual result. Unknown dispositions fail closed as
  ADMITTED and are reported as contract errors.
* A proof fragment carries source/OCR SHA and page, original char_start and
  char_end_exclusive, unchanged block_text and its full_block_text_sha256, plus
  original region_array_index, block_reading_order or equivalent original polygon.
  Every supplied locator must agree. A field's proof is separate from its value.
* ``diagnostic_evidence`` may identify excluded narrative occurrences. It cannot
  select a gold row. Optional harness ``input_blocks`` (same original block
  identity/text/polygon fields) can prove an unselected panel's location; an
  output's unsupported polygon cannot move it out of the evaluated scope.
* ``unattributable_items`` preserves outputs with no actual input-page envelope.

The return value separates a public counts-only ``summary`` from private
``details``. This module does not load gold, run predictions, access a database,
repair OCR, or approve an execution. The caller must separately freeze and check
all input, mapping, application, dictionary and output artifact identities.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
import unicodedata


SOURCE_KEYS = ('source_sha256', 'fixed_ocr_sha256', 'manifest_page')
STATES = ('CORRECT', 'MISMATCH', 'MISSING', 'SOURCE_UNVERIFIED')
POSITIVE = 'POSITIVE_REVIEWED_PANEL'
NEGATIVE = 'NEGATIVE_ADMISSION_BOUNDARY_PAGE'
PREVIOUS = 'PREVIOUSLY_REVIEWED_BUT_OUT_OF_SCOPE'
UNREVIEWED = 'UNJUDGED_NOT_ORIGINAL_REVIEWED'
_MISSING = object()
_FIELDS = {
    'value': ('decimal_value', 'decimal', ('value',)),
    'raw_value_retained': ('raw_value', 'text', ('value',)),
    'unit': ('unit', 'unit', ('unit',)),
    'raw_unit_retained': ('raw_unit', 'compact', ('unit',)),
    'source_type': ('source_type', 'text', ('item_code', 'item_name', 'specimen', 'selected_printed_report_panel')),
    'printed_specimen': ('specimen_raw', 'compact', ('specimen',)),
    'sampling_local_datetime': ('sampling.local_datetime', 'datetime', ('sampling_label', 'sampling_time')),
    'sampling_raw_time_retained': ('sampling.raw', 'compact', ('sampling_label', 'sampling_time')),
    'sampling_role': ('sampling.role', 'text', ('sampling_label', 'sampling_time')),
    'sampling_precision': ('sampling.precision', 'text', ('sampling_time',)),
    'reporting_local_datetime': ('reporting.local_datetime', 'datetime', ('reporting_label', 'reporting_time')),
    'reporting_raw_time_retained': ('reporting.raw', 'compact', ('reporting_label', 'reporting_time')),
    'reporting_role': ('reporting.role', 'text', ('reporting_label', 'reporting_time')),
    'reporting_precision': ('reporting.precision', 'text', ('reporting_time',)),
    'unconfirmed_timezone_preserved': (None, None, ('manually_reviewed_panel_absence_assertion',)),
    'utc_not_invented': (None, None, ('manually_reviewed_panel_absence_assertion',)),
    'meal_context': ('meal_context.status', 'text', ('meal_context_or_reviewed_absence_assertion',)),
    'meal_context_basis': (None, None, ('meal_context_or_reviewed_absence_assertion',)),
}


def _get(value, path):
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _source_equal(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and all(
        key in left and key in right and type(left[key]) is type(right[key]) and left[key] == right[key]
        for key in SOURCE_KEYS)


def _page_key(source):
    if (not isinstance(source, dict) or not isinstance(source.get('source_sha256'), str)
            or type(source.get('manifest_page')) is not int or source['manifest_page'] < 1):
        return None
    return source['source_sha256'], source['manifest_page']


def _polygon(value):
    if not isinstance(value, list) or len(value) < 3:
        return None
    points = []
    for point in value:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point)):
            return None
        points.append(tuple(point))
    return points


def _same_polygon(left, right):
    a, b = _polygon(left), _polygon(right)
    if not a or not b or len(a) != len(b):
        return False
    return any(all(abs(x - y) <= 0.000001 for p, q in zip(a, b[offset:] + b[:offset])
                   for x, y in zip(p, q)) for offset in range(len(b)))


def _same_block(claim, reference):
    located = False
    for key in ('region_array_index', 'block_reading_order'):
        if key in claim:
            if type(claim[key]) is not int or key not in reference or claim[key] != reference[key]:
                return False
            located = True
    if 'original_normalized_polygon' in claim:
        if not _same_polygon(claim['original_normalized_polygon'], reference.get('original_normalized_polygon')):
            return False
        located = True
    return located


def _claim_text(claim):
    if not isinstance(claim, dict) or not isinstance(claim.get('block_text'), str):
        return None
    text = claim['block_text']
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != claim.get('full_block_text_sha256'):
        return None
    start, end = claim.get('char_start'), claim.get('char_end_exclusive')
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return None
    return text


def _fragment_matches(claim, fragment, evidence, envelope, *, overlap=False):
    text = _claim_text(claim)
    if (text is None or not _source_equal(claim, envelope) or not _source_equal(claim, evidence)
            or claim['full_block_text_sha256'] != fragment.get('full_block_text_sha256')
            or not _same_block(claim, fragment)):
        return False
    start, end = fragment['char_start'], fragment['char_end_exclusive']
    wanted = fragment['raw_fixed_ocr_fragment']
    if text[start:end] != wanted:
        return False
    left, right = claim['char_start'], claim['char_end_exclusive']
    if overlap:
        return max(left, start) < min(right, end)
    if not left <= start < end <= right:
        return False
    return (left, right) == (start, end) or text[left:right].count(wanted) == 1


def _prove(claims, evidence, envelope):
    if not isinstance(claims, list) or not isinstance(evidence, dict):
        return False
    expected = evidence.get('fixed_ocr_fragments', [])
    if not expected or len(claims) != len(expected):
        return False
    # One-to-one proof covering all declared fragments; no unrelated extra block.
    edges = [[j for j, fragment in enumerate(expected)
              if _fragment_matches(claim, fragment, evidence, envelope)] for claim in claims]
    owners = {}

    def assign(index, seen):
        for target in edges[index]:
            if target in seen:
                continue
            seen.add(target)
            if target not in owners or assign(owners[target], seen):
                owners[target] = index
                return True
        return False

    return all(assign(index, set()) for index in range(len(claims)))


def _row_proven(item, row, evidence, envelope):
    claims = item.get('row_evidence', {})
    if not isinstance(claims, dict):
        return False
    refs = row['field_evidence_refs']
    own = lambda role: _prove(claims.get(role), evidence.get(refs.get(role)), envelope)
    return own('value') and (own('item_code') or own('item_name'))


def _field_proven(metric, item, row, evidence, envelope):
    proofs = item.get('field_evidence', {})
    if not isinstance(proofs, dict):
        proofs = {}
    absent = row.get('negative_assertions_from_original_review', {})
    for role in _FIELDS[metric][2]:
        if role == 'selected_printed_report_panel':
            continue  # Already proved by the selected value/item row association.
        if role == 'manually_reviewed_panel_absence_assertion':
            if absent.get('no_printed_timezone_or_offset_on_this_panel') is not True:
                return False
            continue
        if role == 'meal_context_or_reviewed_absence_assertion':
            if row['expected']['meal_context']['status'] == 'NOT_STATED':
                if absent.get('no_meal_condition_on_this_panel') is not True:
                    return False
                continue
            role = 'meal_context'
        if not _prove(proofs.get(role), evidence.get(row['field_evidence_refs'].get(role)), envelope):
            return False
    return True


def _text(value, *, compact=False):
    if not isinstance(value, str):
        raise ValueError('not text')
    value = unicodedata.normalize('NFKC', value).strip()
    return ''.join(value.split()) if compact else value


def _normalize(value, rule):
    if rule == 'decimal':
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError('not numeric')
        result = Decimal(_text(str(value)))
        if not result.is_finite():
            raise ValueError('not finite')
        return result
    if rule == 'datetime':
        value = _text(value)
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}', value):
            raise ValueError('not full local seconds')
        return datetime.fromisoformat(value)
    if rule in ('compact', 'unit'):
        value = _text(value, compact=True)
        return 'mmol/L' if rule == 'unit' and value == 'mmol/l' else value
    if rule == 'strict':
        return type(value), value
    return _text(value)


def _metric_parts(metric):
    path, normalization, _proof = _FIELDS[metric]
    if path is not None:
        return [(path, normalization)]
    if metric == 'unconfirmed_timezone_preserved':
        return [(f'{role}.{key}', 'strict') for role in ('sampling', 'reporting') for key in (
            'printed_timezone', 'printed_utc_offset', 'timezone_status', 'timezone_name', 'utc_offset')]
    if metric == 'utc_not_invented':
        return [(f'{role}.utc_datetime', 'strict') for role in ('sampling', 'reporting')]
    return [('meal_context.basis', 'text'), ('meal_context.actual_preparation_independently_verified', 'strict'),
            ('meal_context.clock_inference_allowed', 'strict')]


def _semantic_status(metric, item, expected):
    abstained = item.get('abstained_fields', [])
    if not isinstance(abstained, list) or any(not isinstance(key, str) or key not in _FIELDS for key in abstained):
        return 'MISSING'
    if metric in abstained:
        return 'MISSING'
    parts = [(_get(item.get('values', {}), path), _get(expected, path), rule) for path, rule in _metric_parts(metric)]
    if any(want is _MISSING for _actual, want, _rule in parts):
        raise ValueError('Gold is missing a required field')
    if any(actual is _MISSING or (actual is None and want is not None) or
           (isinstance(actual, str) and not actual.strip()) for actual, want, _rule in parts):
        return 'MISSING'
    if any(isinstance(actual, str) and actual == 'UNKNOWN' and want != 'UNKNOWN' for actual, want, _rule in parts):
        return 'MISSING'
    try:
        correct = all(_normalize(actual, rule) == _normalize(want, rule) for actual, want, rule in parts)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        correct = False
    return 'AGREEMENT' if correct else 'MISMATCH'


def _prediction_order(wrapper):
    value = wrapper['item'].get('prediction_id')
    return (0, value, wrapper['index']) if isinstance(value, str) and value.strip() else (1, '', wrapper['index'])


def _row_claims(item):
    proofs = item.get('row_evidence', {})
    if not isinstance(proofs, dict) or not isinstance(proofs.get('value'), list) or not proofs['value']:
        return []
    return [claim for role in ('value', 'item_code', 'item_name') if isinstance(proofs.get(role), list)
            for claim in proofs[role]]


def _trusted_polygons(wrapper):
    claims = _row_claims(wrapper['item'])
    if not claims or not wrapper['valid_source']:
        return []
    polygons = []
    for claim in claims:
        text = _claim_text(claim)
        if text is None or not _source_equal(claim, wrapper['source']):
            return []
        matches = [block for block in wrapper['blocks'] if isinstance(block, dict)
                   and _source_equal(block, wrapper['source']) and block.get('block_text') == text and _same_block(claim, block)]
        distinct = {tuple(_polygon(block.get('original_normalized_polygon')) or []) for block in matches}
        if len(distinct) != 1 or not next(iter(distinct)):
            return []
        polygons.append(next(iter(distinct)))
    return polygons


def _outside_panels(wrapper, rows):
    polygons = _trusted_polygons(wrapper)
    if not polygons:
        return False
    for points in polygons:
        xs, ys = zip(*points)
        for row in rows:
            left, top, right, bottom = row['panel_scope']['original_normalized_bbox']
            if max(min(xs), left) < min(max(xs), right) and max(min(ys), top) < min(max(ys), bottom):
                return False
    return True


def _case_proven(wrapper, case, evidence):
    if not wrapper['valid_source']:
        return False
    claims = _row_claims(wrapper['item'])
    diagnostic = wrapper['item'].get('diagnostic_evidence', [])
    if isinstance(diagnostic, list):
        claims += diagnostic
    return any(_fragment_matches(claim, fragment, proof, wrapper['source'], overlap=True)
               for ref in case['evidence_refs'] for proof in [evidence.get(ref)] if proof
               for fragment in proof['fixed_ocr_fragments'] for claim in claims)


def _validate_reference(gold, protocol, evidence, coverage):
    rows, negatives, cases = gold['positive_rows'], gold['negative_pages'], gold['negative_cases']
    metrics = protocol['per_field_metrics']
    if ([entry['id'] for entry in metrics['metrics']] != list(_FIELDS)
            or metrics['denominator_each'] != len(rows)):
        raise ValueError('Unsupported metric contract or changed field denominator')
    pages, proofs = {}, {}
    for page in coverage['pages']:
        key = _page_key(page)
        if key is None or key in pages or page['status'] not in (POSITIVE, NEGATIVE, PREVIOUS, UNREVIEWED):
            raise ValueError('Invalid or repeated coverage page')
        pages[key] = page
    for proof in evidence['evidence']:
        if proof['evidence_id'] in proofs:
            raise ValueError('Repeated evidence identity')
        proofs[proof['evidence_id']] = proof
    for collection, id_key in ((rows, 'gold_id'), (negatives, 'negative_page_id'), (cases, 'case_id')):
        if len({row[id_key] for row in collection}) != len(collection):
            raise ValueError('Repeated gold identity')
    for row in rows + negatives:
        page = pages.get(_page_key(row['source']))
        status = POSITIVE if 'gold_id' in row else NEGATIVE
        if page is None or page['status'] != status or not _source_equal(page, row['source']):
            raise ValueError('Gold and coverage source identity disagree')
    negative_ids = {row['negative_page_id'] for row in negatives}
    for case in cases:
        if case['negative_page_id'] not in negative_ids or any(ref not in proofs for ref in case['evidence_refs']):
            raise ValueError('Invalid negative-case evidence')
    for row in rows:
        if any(ref is not None and ref not in proofs for ref in row['field_evidence_refs'].values()):
            raise ValueError('Invalid positive evidence')
    return pages, proofs


def _collect_predictions(predictions, coverage):
    if predictions.get('schema_version') != 1 or not isinstance(predictions.get('pages'), list):
        raise ValueError('Expected neutral prediction contract version 1')
    observed, wrappers, errors = defaultdict(list), [], Counter()
    execution_details = []

    def append(item, source, key, valid, blocks):
        if not isinstance(item, dict):
            item = {}
            errors['non_object_item'] += 1
        disposition = item.get('disposition')
        if disposition not in ('ADMITTED', 'EXCLUDED', 'ABSTAINED'):
            disposition = 'ADMITTED'
            errors['invalid_disposition'] += 1
        abstained = item.get('abstained_fields', [])
        if not isinstance(abstained, list) or any(not isinstance(key, str) or key not in _FIELDS for key in abstained):
            errors['invalid_abstained_fields'] += 1
        valid = valid and ('source' not in item or _source_equal(item['source'], source))
        wrappers.append({'item': item, 'source': source, 'page': key, 'valid_source': valid,
                         'disposition': disposition, 'blocks': blocks, 'index': len(wrappers)})

    for page in predictions['pages']:
        if not isinstance(page, dict):
            raise ValueError('Every page envelope must be an object')
        source = page.get('source', {})
        if not isinstance(source, dict):
            source = {}
            errors['invalid_page_source'] += 1
        raw_key = _page_key(source)
        key = raw_key if raw_key in coverage else None
        valid = key is not None and _source_equal(source, coverage[key])
        if key is None:
            errors['unknown_page_envelope'] += 1
        elif not valid:
            errors['wrong_ocr_envelope'] += 1
        if key is not None:
            observed[key].append((page.get('status'), valid))
        reported_errors = page.get('errors', [])
        if not isinstance(reported_errors, list):
            reported_errors = [reported_errors]
        execution_details.append({'source_sha256': source.get('source_sha256'),
                                  'manifest_page': source.get('manifest_page'), 'input_status': page.get('status'),
                                  'valid_envelope': valid, 'errors': deepcopy(reported_errors)})
        items = page.get('items', [])
        if not isinstance(items, list):
            raise ValueError('Page items must preserve their output array')
        blocks = page.get('input_blocks', [])
        if not isinstance(blocks, list):
            blocks = []
            errors['invalid_input_blocks'] += 1
        for item in items:
            append(item, source, key, valid, blocks)
    for item in predictions.get('unattributable_items', []):
        append(item, {}, None, False, [])
    executions = {}
    for key in coverage:
        states = observed[key]
        if not states:
            executions[key] = 'NOT_RUN'
            execution_details.append({'source_sha256': key[0], 'manifest_page': key[1],
                                      'input_status': 'NOT_RUN', 'valid_envelope': False, 'errors': []})
        elif len(states) != 1:
            executions[key] = 'INVALID_ENVELOPE'
            errors['duplicate_page_envelope'] += 1
        elif states[0][1] and states[0][0] in ('COMPLETE', 'FAILED', 'NOT_RUN'):
            executions[key] = states[0][0]
        else:
            executions[key] = 'INVALID_ENVELOPE'
    return wrappers, executions, errors, execution_details


def score_predictions(gold, protocol, evidence, coverage, predictions):
    """Score immutable neutral outputs; return public counts and private traces."""
    pages, proofs = _validate_reference(gold, protocol, evidence, coverage)
    wrappers, executions, contract_errors, execution_details = _collect_predictions(predictions, pages)
    rows = gold['positive_rows']
    by_page = defaultdict(list)
    for row in rows:
        by_page[_page_key(row['source'])].append(row)
    row_candidates = defaultdict(list)
    for wrapper in wrappers:
        if wrapper['disposition'] != 'ADMITTED' or not wrapper['valid_source']:
            continue
        matched = [row for row in by_page[wrapper['page']]
                   if _row_proven(wrapper['item'], row, proofs, wrapper['source'])]
        if len(matched) == 1:
            row_candidates[matched[0]['gold_id']].append(wrapper)
        elif len(matched) > 1:
            contract_errors['ambiguous_source_row'] += 1
    paired, duplicates = {}, set()
    for row in rows:
        candidates = sorted(row_candidates[row['gold_id']], key=_prediction_order)
        if candidates:
            paired[row['gold_id']] = candidates[0]
            duplicates.update(wrapper['index'] for wrapper in candidates[1:])
    fields = {metric: {'denominator': len(rows), **dict.fromkeys(STATES, 0), 'semantic_agreement': 0}
              for metric in _FIELDS}
    details = {'rows': [], 'items': [], 'negative_pages': [], 'negative_cases': [], 'execution': execution_details}
    meal = {status: {'denominator': 0, **dict.fromkeys(STATES, 0), 'confusion': dict.fromkeys(
        ('FASTING', 'NOT_STATED', 'UNKNOWN', 'OTHER', 'MISSING'), 0)} for status in ('FASTING', 'NOT_STATED')}
    for row in rows:
        wrapper = paired.get(row['gold_id'])
        results = {}
        for metric in _FIELDS:
            state = _semantic_status(metric, wrapper['item'], row['expected']) if wrapper else 'MISSING'
            if state == 'AGREEMENT':
                fields[metric]['semantic_agreement'] += 1
                state = 'CORRECT' if _field_proven(metric, wrapper['item'], row, proofs, wrapper['source']) else 'SOURCE_UNVERIFIED'
            fields[metric][state] += 1
            results[metric] = state
        expected_meal = row['expected']['meal_context']['status']
        bucket = meal[expected_meal]
        bucket['denominator'] += 1
        bucket[results['meal_context']] += 1
        actual_meal = _get(wrapper['item'].get('values', {}), 'meal_context.status') if wrapper else _MISSING
        label = 'MISSING' if actual_meal is _MISSING or actual_meal is None else actual_meal
        if not isinstance(label, str) or label not in bucket['confusion']:
            label = 'OTHER'
        bucket['confusion'][label] += 1
        details['rows'].append({'gold_id': row['gold_id'], 'panel_id': row['panel_id'],
                               'prediction_id': wrapper['item'].get('prediction_id') if wrapper else None,
                               'output_index': wrapper['index'] if wrapper else None, 'fields': results})
    positive_counts = dict.fromkeys(('admitted_items', 'duplicates', 'other_extras', 'unverified_row_extras'), 0)
    outside = dict.fromkeys(('unreviewed_admissions', 'previously_reviewed_admissions', 'unselected_panel_admissions', 'retained_items'), 0)
    negative_items = defaultdict(list)
    paired_indices = {wrapper['index'] for wrapper in paired.values()}
    unattributable = 0
    for wrapper in wrappers:
        key, disposition = wrapper['page'], wrapper['disposition']
        partition = pages[key]['status'] if key is not None else None
        admitted = disposition == 'ADMITTED'
        if admitted and not wrapper['valid_source']:
            unattributable += 1
        if partition in (UNREVIEWED, PREVIOUS):
            outside['retained_items'] += 1
            if admitted:
                outside['unreviewed_admissions' if partition == UNREVIEWED else 'previously_reviewed_admissions'] += 1
            outcome = 'OUT_OF_SCOPE_UNJUDGED' if partition == UNREVIEWED else 'OUT_OF_SCOPE_PREVIOUSLY_REVIEWED'
        elif partition == NEGATIVE:
            negative_items[key].append(wrapper)
            outcome = 'NEGATIVE_PAGE_ADMISSION' if admitted else disposition
        elif partition == POSITIVE and admitted:
            if wrapper['index'] in paired_indices:
                outcome = 'PAIRED'
            elif wrapper['index'] in duplicates:
                positive_counts['duplicates'] += 1
                outcome = 'DUPLICATE'
            elif _outside_panels(wrapper, by_page[key]):
                outside['unselected_panel_admissions'] += 1
                outside['retained_items'] += 1
                outcome = 'OUT_OF_SCOPE_UNSELECTED_PANEL'
            elif _trusted_polygons(wrapper):
                positive_counts['other_extras'] += 1
                outcome = 'POSITIVE_PANEL_EXTRA'
            else:
                positive_counts['unverified_row_extras'] += 1
                outcome = 'UNVERIFIABLE_ROW_EXTRA'
            if outcome != 'OUT_OF_SCOPE_UNSELECTED_PANEL':
                positive_counts['admitted_items'] += 1
        else:
            outcome = 'UNATTRIBUTABLE_ADMISSION' if admitted else disposition
        details['items'].append({'output_index': wrapper['index'], 'prediction_id': wrapper['item'].get('prediction_id'),
                                 'source_sha256': wrapper['source'].get('source_sha256'),
                                 'manifest_page': wrapper['source'].get('manifest_page'), 'outcome': outcome,
                                 'source_identity_verified': wrapper['valid_source']})
    negatives = {'denominator': len(gold['negative_pages']), 'false_admission_pages': 0, 'false_admitted_items': 0,
                 'completed_without_admission': 0, 'unassessed_execution': 0, 'unmatched_case_items': 0}
    case_counts = {'denominator': len(gold['negative_cases']), **dict.fromkeys(
        ('FALSE_ADMISSION', 'SAFE_EXCLUSION', 'NO_ADMISSION', 'UNASSESSED_EXECUTION'), 0)}
    case_groups = defaultdict(list)
    for case in gold['negative_cases']:
        case_groups[case['negative_page_id']].append(case)
    for page in gold['negative_pages']:
        key = _page_key(page['source'])
        emitted = negative_items[key]
        admitted = [wrapper for wrapper in emitted if wrapper['disposition'] == 'ADMITTED']
        completed = executions[key] == 'COMPLETE'
        negatives['false_admission_pages'] += bool(admitted)
        negatives['false_admitted_items'] += len(admitted)
        negatives['completed_without_admission'] += completed and not admitted
        negatives['unassessed_execution'] += not completed
        attributed = set()
        for case in case_groups[page['negative_page_id']]:
            related = [wrapper for wrapper in emitted if _case_proven(wrapper, case, proofs)]
            wrong = [wrapper for wrapper in related if wrapper['disposition'] == 'ADMITTED']
            attributed.update(wrapper['index'] for wrapper in wrong)
            status = ('FALSE_ADMISSION' if wrong else 'UNASSESSED_EXECUTION' if not completed else
                      'SAFE_EXCLUSION' if any(w['disposition'] == 'EXCLUDED' for w in related) else 'NO_ADMISSION')
            case_counts[status] += 1
            details['negative_cases'].append({'case_id': case['case_id'], 'status': status,
                                              'false_admissions': len(wrong), 'execution': executions[key]})
        negatives['unmatched_case_items'] += sum(wrapper['index'] not in attributed for wrapper in admitted)
        details['negative_pages'].append({'negative_page_id': page['negative_page_id'], 'execution': executions[key],
                                         'false_admitted_items': len(admitted), 'successful_rejection': completed and not admitted})
    for metric in fields.values():
        if sum(metric[state] for state in STATES) != len(rows):
            raise AssertionError('Field denominator changed during scoring')
    execution_counts = Counter(executions.values())
    return {'schema_version': 1, 'summary': {
        'fields': fields, 'source_rows': {'denominator': len(rows), 'recovered': len(paired), 'missing': len(rows) - len(paired)},
        'meal_strata': meal, 'positive_admissions': positive_counts, 'negative_pages': negatives,
        'negative_cases': case_counts, 'out_of_scope': outside, 'unattributable_admissions': unattributable,
        'retained_items': len(wrappers), 'dispositions': dict(Counter(w['disposition'] for w in wrappers)),
        'execution': {'fixed_pages': len(pages), 'fixed_sources': len({key[0] for key in pages}),
                      'complete_pages': execution_counts['COMPLETE'], 'failed_pages': execution_counts['FAILED'],
                      'not_run_pages': execution_counts['NOT_RUN'], 'invalid_envelope_pages': execution_counts['INVALID_ENVELOPE'],
                      'reported_errors': sum(len(page['errors']) for page in execution_details),
                      'contract_errors': dict(contract_errors)},
        'unrepresented_positive_categories': {key: {'denominator': 0, 'status': 'NOT_EVALUATED'}
                                               for key, count in gold.get('unrepresented_positive_categories', {}).items() if count == 0},
        'method': 'Source-position pairing; separate per-field fidelity and bounded admission strata; no aggregate clinical accuracy.',
    }, 'details': details}
