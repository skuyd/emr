"""Public, deterministic release evaluation with explicitly supplied snapshots.

The fixed extraction gate remains authoritative. This adapter reuses its actual
OCR extraction results, then exercises the production comparison/validation
functions without a database, private sources, or activating a release.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date
import json
from pathlib import Path
import time
from types import SimpleNamespace

from tools.phase_two_evaluation import (
    EvaluationError, FIELDS, _assignment, _intercepted, capture_parser_identity,
    digest, evaluate_predictions, file_digest,
)

from . import extraction, regression
from .comparison import comparable_cell
from .dictionary import IndicatorDictionary, rules_digest
from .models import ResultType
from .numerics import numeric_value
from .quality import QUALITY_POLICY_VERSION
from .validation import TREND_BLOCKING_ISSUES


_ROOT = Path(__file__).resolve().parents[2]
_CONTEXT_PATH = Path(__file__).resolve().parent / 'dictionaries/phase-two-release-evaluation.json'


def _dataset():
    fixed, fixed_hash = regression._load_corpus()
    context = json.loads(_CONTEXT_PATH.read_text(encoding='utf-8'))
    if context.get('schema_version') != 1 or context.get('dataset_kind') != 'SYNTHETIC':
        raise EvaluationError('Invalid public release evaluation corpus')
    identifiers = [item['case_id'] for item in context['cases']]
    if len(identifiers) != len(set(identifiers)) or any(not item.startswith('release:') for item in identifiers):
        raise EvaluationError('Duplicate or invalid release case identity')
    hashes = {'fixed_parser': fixed_hash, 'release_context': file_digest(_CONTEXT_PATH)}
    return fixed, context, hashes


def _extra_case(item, context):
    table = context['table']
    rows = [[.02, [[.05, table['specimen_text']]]],
            [table['header_y'], [[x, value] for x, value in zip(table['x'], table['headers'])]]]
    expected = []
    for index, row in enumerate(item['rows']):
        y = table['first_y'] + index * table['row_step']
        values = [row['raw_name'], row['raw_value'], row['raw_unit']]
        rows.append([y, [[x, value] for x, value in zip(table['x'], values)]])
        expected.append({**row, 'page_number': 1, 'specimen': table['specimen'],
                         'reference_range_raw': '', 'report_flag_raw': '',
                         'source_bounds': {field: [x, y, x + .07, y + .025]
                             for field, x in zip(('raw_name', 'raw_value', 'raw_unit'), table['x'])}})
    return {'pages': [{'page_number': 1, 'rows': rows}], 'expected': expected}


def frozen_release_cases():
    """Return candidate-independent cases for current or historical extractors.

    Each record contains ``case_id``, ``source_file_hash``, ``pages`` (OcrPage
    tuples), fixed ``expected`` rows and supplied synthetic ``context``. Dates
    are context, not metadata-extraction truth. All dictionaries use this same
    full corpus, including an exact legacy dictionary's additional assessment.
    """
    fixed, context, _hashes = _dataset()
    specifications = []
    for target in fixed['targets']:
        for value in target['results']:
            specifications.append(('target:' + target['code'] + ':' + value[1],
                regression._target_table(fixed, target, value, legacy=False), {}))
    for case in fixed['layout_cases']:
        specifications.append(('layout:' + case['id'], case, {}))
    for target in fixed['targets']:
        if target['legacy_name'] is not None:
            for value in target['results']:
                specifications.append(('legacy:' + target['code'] + ':' + value[1],
                    regression._target_table(fixed, target, value, legacy=True), {}))
    for case in context['cases']:
        supplied = {**context['extra_context'], 'report_date': case['report_date'],
                    'patient_group_id': case['patient_group_id']}
        specifications.append((case['case_id'], _extra_case(case, context), supplied))
    output = []
    for case_id, specification, supplied in specifications:
        supplied = {**context['default_context'], 'patient_group_id': 'isolated:' + case_id,
                    'report_group_id': case_id, **supplied}
        date.fromisoformat(supplied['report_date'])
        source_hash = digest({'case_id': case_id, 'pages': specification['pages'], 'context': supplied})
        output.append({'case_id': case_id, 'source_file_hash': source_hash,
            'pages': tuple(regression._page(page) for page in specification['pages']),
            'expected': deepcopy(specification['expected']), 'context': supplied})
    return tuple(output)


def _frozen_truth(cases):
    annotations = {'review_status': 'adjudicated', 'rows': [], 'reports': []}
    classification = {'files': [], 'reports': []}
    for case in cases:
        group = case['context']['report_group_id']
        report = {'report_group_id': group, 'target_rows_complete': True}
        annotations['reports'].append(report)
        classification['reports'].append(report)
        classification['files'].append({'source_file_hash': case['source_file_hash'],
            'document_type': 'laboratory', 'report_group_ids': [group]})
        for index, expected in enumerate(case['expected']):
            sources = {name: {'region': bounds, 'page_number': expected['page_number'], 'granularity': 'field'}
                       for name, bounds in expected.get('source_bounds', {}).items()}
            annotations['rows'].append({**expected, 'annotation_id': f'{case["case_id"]}:{index}',
                'source_file_hash': case['source_file_hash'], 'report_group_id': group,
                'row_index': index, 'report_date': None, 'source_verified': True, 'field_sources': sources})
    return annotations, classification


def _observation(row, case, index, dictionary):
    # Only context which the public fixture declares is supplied. In particular,
    # do not repair extracted names, values, units, types or specimen from gold.
    values = deepcopy(row)
    source = case['source_file_hash']
    supplied = case['context']
    document = SimpleNamespace(pk=source, patient_id=supplied['patient_group_id'], deleted_at=None)
    version = SimpleNamespace(pk=source, document=document, document_id=source,
                              diagnostics={'quality_policy': QUALITY_POLICY_VERSION})
    defaults = {'raw_name': '', 'standard_code': '', 'standard_name': '', 'raw_value': '',
        'raw_unit': '', 'result_type': 'STATUS', 'specimen': '', 'reference_range_raw': '',
        'capability_level': 'SEARCH_ONLY', 'quality_issues': [], 'field_evidence': {},
        'confidence': 0, 'reading_order': index}
    values = {**defaults, **values, 'pk': f'{source}:{index}', 'parsing_version_id': source,
        'parsing_version': version, 'dictionary_version': dictionary.version,
        'mapping_dictionary_version': dictionary.version,
        'method_raw': supplied['method'], 'observation_date': date.fromisoformat(supplied['report_date']),
        'date_verified': True, 'value_sources': {}, 'revision_conflict': False,
        'reported_error': False, 'resolved_issues': []}
    values['evidence'] = SimpleNamespace(confidence=values['confidence'])
    return SimpleNamespace(**values)


def _rule_context_applies(rule, observation, peers, cells):
    """Count explicit applicable contexts, never manufacture expected outcomes.

    Trigger counts below always come from the actual production comparison.
    Context coverage does not assert that a rule's threshold is clinically valid.
    """
    required = ('id', 'version', 'reviewed_by', 'rationale', 'code', 'specimen', 'method')
    if not all(rule.get(name) for name in required) or any(rule.get(name) != getattr(observation, name)
            for name in ('specimen',)) or rule['code'] != observation.standard_code or rule['method'] != observation.method_raw:
        return False
    if observation.result_type != ResultType.NUMERIC or numeric_value(observation.raw_value) is None:
        return False
    cell = cells[observation.pk]
    if {item['code'] for item in cell.quality_issues} & TREND_BLOCKING_ISSUES:
        return False
    kind = rule.get('kind')
    unit = rule.get('source_unit') if kind == 'conversion' else rule.get('unit')
    if extraction._unit_key(unit or '') != extraction._unit_key(observation.raw_unit):
        return False
    if kind == 'conversion':
        factor = numeric_value(rule.get('factor'))
        return bool(cell.comparability != 'insufficient' and factor is not None and factor > 0 and rule.get('target_unit'))
    if kind == 'history_ratio':
        threshold = numeric_value(rule.get('minimum_ratio'))
        if threshold is None or threshold <= 1 or numeric_value(observation.raw_value) <= 0:
            return False
        prior = [item for item in peers if item.observation_date < observation.observation_date
                 and item.standard_code == observation.standard_code and item.specimen == observation.specimen
                 and item.method_raw == observation.method_raw
                 and extraction._unit_key(item.raw_unit) == extraction._unit_key(observation.raw_unit)
                 and item.result_type == ResultType.NUMERIC and numeric_value(item.raw_value) is not None
                 and numeric_value(item.raw_value) > 0
                 and not {issue['code'] for issue in cells[item.pk].quality_issues} & TREND_BLOCKING_ISSUES]
        return bool(prior and sum(item.observation_date == max(previous.observation_date for previous in prior) for item in prior) == 1)
    if kind == 'report_sum':
        components = rule.get('component_codes')
        tolerance = numeric_value(rule.get('absolute_tolerance'))
        if not isinstance(components, list) or not components or len(components) != len(set(components)) or tolerance is None or tolerance < 0:
            return False
        return all(sum(item.standard_code == code and item.pk != observation.pk
                       and item.parsing_version_id == observation.parsing_version_id
                       and item.specimen == observation.specimen and item.method_raw == observation.method_raw
                       and extraction._unit_key(item.raw_unit) == extraction._unit_key(observation.raw_unit)
                       and item.result_type == ResultType.NUMERIC and numeric_value(item.raw_value) is not None
                       and item.confidence >= .95 and not item.quality_issues for item in peers) == 1 for code in components)
    return False


def _downstream(predictions, cases, dictionary, rules):
    by_source = {case['source_file_hash']: case for case in cases}
    groups = defaultdict(list)
    collected = []
    for record in predictions:
        case = by_source[record['source_file_hash']]
        for index, raw in enumerate(record.get('observations', [])):
            observation = _observation(raw, case, index, dictionary)
            groups[case['context']['patient_group_id']].append(observation)
            collected.append((raw, observation, case['case_id']))
    cells = {observation.pk: comparable_cell(observation, previous=groups[observation.parsing_version.document.patient_id],
             dictionary=dictionary, rules=()) for _raw, observation, _case in collected}
    coverage = [{'rule_id': rule.get('id'), 'rule_version': rule.get('version'), 'kind': rule.get('kind'),
                 'assessed_rows': len(collected), 'applicable': 0, 'triggered': 0} for rule in rules]
    conversions = []
    for raw, observation, case_id in collected:
        peers = groups[observation.parsing_version.document.patient_id]
        cell = comparable_cell(observation, previous=peers, dictionary=dictionary, rules=rules) if rules else cells[observation.pk]
        raw.update(observation_date=observation.observation_date.isoformat(),
                   quality_issues=list(cell.quality_issues), trend_eligible=cell.trend_eligible,
                   comparability=cell.comparability)
        for rule, count in zip(rules, coverage):
            applies = _rule_context_applies(rule, observation, peers, cells)
            count['applicable'] += bool(applies)
            triggered = any(issue.get('rule_id') == rule.get('id') and issue.get('rule_version') == rule.get('version')
                            for issue in cell.quality_issues)
            triggered = triggered or bool(cell.rule and cell.rule.get('id') == rule.get('id') and cell.rule.get('version') == rule.get('version'))
            count['triggered'] += bool(triggered)
        if cell.rule:
            conversions.append({'case_id': case_id, 'reading_order': observation.reading_order,
                'rule_id': cell.rule['id'], 'rule_version': cell.rule['version'],
                'value': str(cell.numeric_value), 'unit': cell.unit})
    for count in coverage:
        count.update(not_applicable=count['assessed_rows'] - count['applicable'],
                     unassessed_rows=count['assessed_rows'] - count['applicable'],
                     outcome_accuracy_assessed=False,
                     status='exercised' if count['applicable'] else 'not_evaluated',
                     reason='' if count['applicable'] else 'No fixed source row satisfied this rule and its available context prerequisites.')
    return coverage, conversions


def _routing(cases, predictions):
    indexed = {item['source_file_hash']: item for item in predictions}
    severe = {'denominator': 0, 'intercepted': 0, 'missing': 0}
    normal = {'denominator': 0, 'routed': 0, 'missing': 0}
    for case in cases:
        expected = case['expected']
        actual = indexed.get(case['source_file_hash'], {}).get('observations', [])
        match = _assignment(expected, actual)
        errors = [index for index, row in enumerate(expected) if row.get('must_block')]
        if errors:
            severe['denominator'] += 1
            severe['missing'] += any(index not in match for index in errors)
            severe['intercepted'] += all(index in match and _intercepted(actual[match[index]]) for index in errors)
        for index, row in enumerate(expected):
            if row.get('must_block') is False:
                normal['denominator'] += 1
                normal['missing'] += index not in match
                normal['routed'] += index in match and _intercepted(actual[match[index]])
    severe['recall'] = severe['intercepted'] / severe['denominator'] if severe['denominator'] else None
    normal['routing_rate'] = normal['routed'] / normal['denominator'] if normal['denominator'] else None
    return severe, normal


def score_frozen_predictions(predictions, *, dictionary=None, rules=(), cases=None):
    """Score source-hash records from any extractor against this fixed corpus.

    The caller supplies actual observations/status per source. Missing records
    remain in the full denominator. With no dictionary, only actual historical
    parser output is scored; routing and trend remain unassessed. Missing output
    fields/evidence remain missing, never supplied from the reference labels.
    """
    cases = frozen_release_cases() if cases is None else cases
    predictions = deepcopy(list(predictions))
    for record in predictions:
        record['observations'] = [asdict(row) if is_dataclass(row) else deepcopy(row) for row in record.get('observations', [])]
    annotations, classification = _frozen_truth(cases)
    sources = {case['source_file_hash'] for case in cases}
    if len({item['source_file_hash'] for item in predictions}) != len(predictions) or any(item['source_file_hash'] not in sources for item in predictions):
        raise EvaluationError('Duplicate or foreign release prediction source')
    output_rows = [row for record in predictions for row in record['observations']]
    availability = {field: {'output_rows': len(output_rows),
        'missing_rows': sum(row.get(field) is None for row in output_rows)}
        for field in ('field_evidence', 'specimen', 'quality_issues', 'observation_date', 'method_raw', 'confidence', 'capability_level')}
    if dictionary is None:
        if rules:
            raise EvaluationError('Historical transcription scoring cannot apply current rules without an explicit dictionary')
        coverage, conversions = [], []
        for row in output_rows:
            row.pop('trend_eligible', None)
    else:
        coverage, conversions = _downstream(predictions, cases, dictionary, rules)
    whole = evaluate_predictions(predictions, annotations, classification)
    extra_sources = {case['source_file_hash'] for case in cases if case['case_id'].startswith('release:')}
    extra = evaluate_predictions([record for record in predictions if record['source_file_hash'] in extra_sources],
        {**annotations, 'rows': [row for row in annotations['rows'] if row['source_file_hash'] in extra_sources]},
        {**classification, 'files': [item for item in classification['files'] if item['source_file_hash'] in extra_sources]})
    severe, normal = _routing(cases, predictions)
    if dictionary is None:
        # Historical extraction did not execute today's validation policy.
        # Retain transcription error/field denominators but make downstream
        # numerators explicitly unavailable rather than counting missing policy
        # evidence as a successful interception.
        for section in (whole, whole['successful_subset']):
            for key in ('intercepted', 'unintercepted', 'joint_correct_intercepted',
                        'interception_recall', 'correct_transcription_routed_ratio'):
                section['severe'][key] = None
        for error in whole['errors']:
            error['intercepted'] = None
        severe.update(intercepted=None, recall=None, assessed=0, unassessed=severe['denominator'])
        normal.update(routed=None, routing_rate=None, assessed=0, unassessed=normal['denominator'])
        whole['metrics_sha256'] = digest({key: value for key, value in whole.items() if key != 'metrics_sha256'})
    successful = {**whole['successful_subset'], 'files': sum(item.get('status') == 'success' for item in predictions)}
    return {'whole': whole, 'successful_subset': successful, 'known_error_interception': severe,
            'normal': normal, 'rule_coverage': coverage, 'conversion_outputs': conversions,
            'extra_context_transcription': {key: extra[key] for key in ('files', 'annotated_rows', 'joint', 'fields')},
            'output_availability': availability, 'downstream': {'assessed': dictionary is not None,
                'context_source': 'frozen_synthetic_fixture' if dictionary is not None else None,
                'reason': '' if dictionary is not None else 'Historical extraction only; current validation, routing and trend were not run.'}}


def _predictions(cases, dictionary, observed):
    output = []
    for case in cases:
        if case['case_id'] in observed:
            rows, error_type = observed[case['case_id']]
        else:
            try:
                rows, error_type = extraction.extract_observations(case['pages'], dictionary), None
            except Exception as error:
                rows, error_type = (), type(error).__name__
        output.append({'source_file_hash': case['source_file_hash'],
            'status': 'failed' if error_type else 'success' if rows else 'original_only',
            'observations': [asdict(row) if is_dataclass(row) else deepcopy(row) for row in rows],
            **({'error_type': error_type} if error_type else {})})
    return output


def _baseline_delta(evaluation, baseline_report, corpus_hash):
    if baseline_report.get('identity', {}).get('corpus_sha256') != corpus_hash:
        raise EvaluationError('Frozen release baseline corpus identity differs')
    content = (baseline_report['evaluation'] if baseline_report.get('scope') == 'frozen_historical_parser'
               else {key: value for key, value in baseline_report.items() if key not in {'metrics_sha256', 'execution'}})
    if digest(content) != baseline_report.get('metrics_sha256'):
        raise EvaluationError('Frozen release baseline metrics digest differs')
    previous = baseline_report['evaluation']['whole']
    current = evaluation['whole']
    if previous['files']['total'] != current['files']['total'] or previous['annotated_rows'] != current['annotated_rows']:
        raise EvaluationError('Frozen release baseline denominator differs')
    fields = {}
    for field in FIELDS:
        before, after = previous['fields'][field], current['fields'][field]
        if before['true_positive'] + before['false_negative'] != after['true_positive'] + after['false_negative']:
            raise EvaluationError('Frozen release baseline field denominator differs')
        fields[field] = {name + '_delta': after[name] - before[name] if after[name] is not None and before[name] is not None else None
                         for name in ('precision', 'recall', 'f1')}
    return {'kind': 'frozen_report', 'metrics_sha256': baseline_report['metrics_sha256'],
            'report_sha256': digest({key: value for key, value in baseline_report.items() if key != 'execution'}),
            'identity': deepcopy(baseline_report['identity']), 'fields': fields,
            'regressed_fields': [field for field in FIELDS if fields[field]['recall_delta'] is not None and fields[field]['recall_delta'] < 0]}


def evaluate_release_snapshot(dictionary, rules=(), *, baseline=None, baseline_rules=(), baseline_report=None):
    """Run one extraction per case and bind full metrics to an unpublished snapshot.

    ``baseline`` retains the existing current-parser dictionary comparison.
    ``baseline_report`` additionally compares a frozen historical parser report.
    Execution timing is deliberately outside the deterministic metrics digest.
    """
    if not isinstance(dictionary, IndicatorDictionary) or baseline is not None and not isinstance(baseline, IndicatorDictionary):
        raise TypeError('Release evaluation requires an IndicatorDictionary snapshot')
    started = time.perf_counter()
    rules, baseline_rules = deepcopy(list(rules)), deepcopy(list(baseline_rules))
    if any(not isinstance(rule, dict) for rule in (*rules, *baseline_rules)):
        raise EvaluationError('Release rules must be objects')
    if len({(rule.get('id'), rule.get('version')) for rule in rules}) != len(rules):
        raise EvaluationError('Duplicate release rule identity')
    _fixed, context, corpus_files = _dataset()
    corpus_hash = digest(corpus_files)
    cases = frozen_release_cases()
    parser = capture_parser_identity(_ROOT)
    observed, prior_observed = {}, {}
    report = regression.run_fixed_regression(dictionary, baseline=baseline,
        observer=lambda case_id, case, rows, error_type: observed.__setitem__(case_id, (rows, error_type)),
        baseline_observer=lambda case_id, case, rows, error_type: prior_observed.__setitem__(case_id, (rows, error_type)))
    extraction_finished = time.perf_counter()
    evaluation = score_frozen_predictions(_predictions(cases, dictionary, observed), dictionary=dictionary, rules=rules, cases=cases)
    coverage = evaluation.pop('rule_coverage')
    evaluation['control_gate'] = {'enforced': report['scope'] == 'phase_two', 'scope': report['scope']}
    if evaluation['control_gate']['enforced']:
        normal, severe, extra = (evaluation[key] for key in ('normal', 'known_error_interception', 'extra_context_transcription'))
        if normal['routed'] or normal['missing']:
            report['failures'].append({'case_id': 'release:normal-controls', 'reason': 'downstream_normal_control_routed',
                'routed': normal['routed'], 'missing': normal['missing'], 'denominator': normal['denominator']})
        if severe['intercepted'] != severe['denominator']:
            report['failures'].append({'case_id': 'release:severe-controls', 'reason': 'downstream_severe_control_not_intercepted',
                'intercepted': severe['intercepted'], 'denominator': severe['denominator']})
        wrong_fields = [name for name, values in extra['fields'].items() if values['false_positive'] or values['false_negative']]
        if wrong_fields or extra['joint']['false_positive'] or extra['joint']['false_negative']:
            report['failures'].append({'case_id': 'release:extra-contexts', 'reason': 'release_context_transcription_failed',
                'fields': wrong_fields, 'annotated_rows': extra['annotated_rows']})
        report['passed'] = not report['failures']
    evaluation['baseline'] = None
    if baseline is not None:
        prior = score_frozen_predictions(_predictions(cases, baseline, prior_observed), dictionary=baseline, rules=baseline_rules, cases=cases)
        evaluation['current_parser_baseline'] = prior
    if baseline_report is not None:
        evaluation['baseline'] = _baseline_delta(evaluation, baseline_report, corpus_hash)
        evaluation['baseline'].update(enforced=report['scope'] == 'phase_two',
            enforcement_scope='all_frozen_fields' if report['scope'] == 'phase_two' else 'legacy_gate_only',
            reason='' if report['scope'] == 'phase_two' else 'Exact legacy rollback retains its original gate; additional Phase Two coverage and full-corpus deltas are reported without claiming legacy coverage.')
        if evaluation['baseline']['enforced']:
            for field in evaluation['baseline']['regressed_fields']:
                report['failures'].append({'case_id': 'frozen-baseline:' + field, 'reason': 'frozen_field_recall_decreased', 'field': field})
                report['passed'] = False
    identity = {'corpus_sha256': corpus_hash, 'corpus_files': corpus_files,
        'dictionary_version': dictionary.version, 'dictionary_sha256': dictionary.content_hash,
        'dictionary_definition_sha256': regression._definition_digest(dictionary), 'rules_sha256': rules_digest(rules),
        'parser_sha256': parser['sha256'], 'parser_files': parser['files'], 'runtime': parser['runtime'],
        'tier_coverage_sha256': parser['tier_coverage_sha256']}
    if baseline is not None:
        identity['current_parser_baseline'] = {'dictionary_version': baseline.version,
            'dictionary_sha256': baseline.content_hash, 'dictionary_definition_sha256': regression._definition_digest(baseline),
            'rules_sha256': rules_digest(baseline_rules)}
    if capture_parser_identity(_ROOT) != parser:
        raise EvaluationError('Parser dependencies changed during release evaluation')
    if _dataset()[2] != corpus_files:
        raise EvaluationError('Public release corpus changed during evaluation')
    report.update(evaluation=evaluation, identity=identity, rule_coverage=coverage)
    report['limits'] = [*report['limits'], *context['limits']]
    report['metrics_sha256'] = digest(report)
    report['execution'] = {'total_seconds': time.perf_counter() - started,
        'fixed_gate_seconds': extraction_finished - started, 'source_kind': 'public_fixed_ocr',
        'private_sources_used': False, 'metadata_extraction_assessed': False, 'persistence_assessed': False}
    return report
