"""Public fixed-data publication evaluation; no database or patient inputs."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
from pathlib import Path

import pytest

from apps.labs.dictionary import load_dictionary, rules_digest


ROOT = Path(__file__).resolve().parents[2]


def evaluator():
    assert importlib.util.find_spec('apps.labs.release_evaluation') is not None, 'release snapshot evaluation is missing'
    return importlib.import_module('apps.labs.release_evaluation')


@pytest.fixture
def dictionary():
    # An unpublished version must execute the supplied snapshot without registry access.
    return replace(load_dictionary(ROOT / 'apps/labs/dictionaries/phase-two.json'), version='synthetic-unpublished')


def history_rule(**changes):
    return {
        'id': 'synthetic-history', 'version': '1', 'kind': 'history_ratio',
        'code': 'LAB_HGB', 'specimen': 'BLOOD', 'method': 'SYNTHETIC-RELEASE-METHOD',
        'unit': 'g/L', 'minimum_ratio': '2', 'reviewed_by': 'synthetic-reviewer',
        'rationale': 'Fixed synthetic arithmetic exercise only', 'evidence': 'public synthetic fixture',
        **changes,
    }


def test_unpublished_release_reports_full_metrics_and_retains_fixed_gate(dictionary):
    report = evaluator().evaluate_release_snapshot(dictionary)
    assert report['passed'], report['failures']
    assert report['counts']['target_positive']['total'] == 125
    assert report['counts']['normal']['total'] == 98
    assert report['counts']['severe']['total'] == 9
    whole = report['evaluation']['whole']
    assert whole['files']['total'] >= 550
    assert whole['joint']['true_positive'] > 550
    assert whole['joint']['false_positive'] == whole['joint']['false_negative'] == 0
    assert whole['fields']['raw_value']['precision'] == whole['fields']['raw_value']['recall'] == 1
    assert whole['fields']['raw_value']['f1'] == 1
    assert whole['severe']['error_rate'] == 0
    assert 0 < whole['trend']['eligible'] < whole['trend']['assessed']
    assert report['evaluation']['successful_subset']['joint'] == whole['joint']
    assert report['identity']['rules_sha256'] == rules_digest([])
    assert report['execution']['total_seconds'] >= 0


def test_execution_timing_does_not_change_deterministic_metrics(dictionary):
    first = evaluator().evaluate_release_snapshot(dictionary)
    second = evaluator().evaluate_release_snapshot(dictionary)
    assert first['metrics_sha256'] == second['metrics_sha256']
    assert {key: value for key, value in first.items() if key != 'execution'} == {
        key: value for key, value in second.items() if key != 'execution'}
    assert json.loads(json.dumps(first)) == first


def test_failed_extraction_preserves_full_annotation_denominator(dictionary, monkeypatch):
    from apps.labs import extraction
    module = evaluator()
    baseline = module.evaluate_release_snapshot(dictionary)

    def fail(pages, dictionary):
        raise RuntimeError('synthetic failure, never use this text in public results')

    monkeypatch.setattr(extraction, 'extract_observations', fail)
    report = module.evaluate_release_snapshot(dictionary)
    whole = report['evaluation']['whole']
    assert not report['passed']
    assert whole['files']['failed'] == whole['files']['total']
    assert whole['annotated_rows'] == baseline['evaluation']['whole']['annotated_rows']
    assert whole['fields']['raw_value']['recall'] == 0
    assert report['evaluation']['successful_subset']['annotated_rows'] == 0
    assert 'synthetic failure' not in json.dumps(report)


def test_duplicate_predictions_decrease_precision_without_changing_truth(dictionary, monkeypatch):
    from apps.labs import extraction
    module = evaluator()
    original = extraction.extract_observations
    monkeypatch.setattr(extraction, 'extract_observations', lambda pages, dictionary: (
        lambda rows: (*rows, rows[0]) if len(rows) == 1 else rows)(original(pages, dictionary)))
    report = module.evaluate_release_snapshot(dictionary)
    whole = report['evaluation']['whole']
    assert whole['joint']['recall'] == 1
    assert whole['joint']['false_positive'] > 0
    assert whole['joint']['precision'] < 1
    assert not report['passed']


def test_candidate_rules_execute_against_fixed_history_context(dictionary):
    module = evaluator()
    without = module.evaluate_release_snapshot(dictionary)
    with_rule = module.evaluate_release_snapshot(dictionary, [history_rule()])
    coverage, = with_rule['rule_coverage']
    assert coverage['rule_id'] == 'synthetic-history'
    assert coverage['applicable'] >= 1
    assert coverage['triggered'] >= 1
    assert with_rule['evaluation']['whole']['trend']['eligible'] < without['evaluation']['whole']['trend']['eligible']
    assert with_rule['identity']['rules_sha256'] != without['identity']['rules_sha256']
    assert with_rule['identity']['corpus_sha256'] == without['identity']['corpus_sha256']


def test_uncovered_rule_is_reported_unassessed_not_verified(dictionary):
    report = evaluator().evaluate_release_snapshot(dictionary, [history_rule(method='UNSEEN-METHOD')])
    coverage, = report['rule_coverage']
    assert coverage['applicable'] == coverage['triggered'] == 0
    assert coverage['status'] == 'not_evaluated'
    assert coverage['reason']


def test_frozen_baseline_rejects_different_corpus_and_reports_recall_delta(dictionary):
    module = evaluator()
    baseline = module.evaluate_release_snapshot(dictionary)
    current = module.evaluate_release_snapshot(dictionary, baseline_report=baseline)
    assert current['evaluation']['baseline']['fields']['raw_value']['recall_delta'] == 0
    changed = deepcopy(baseline)
    changed['identity']['corpus_sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='corpus'):
        module.evaluate_release_snapshot(dictionary, baseline_report=changed)

    changed = deepcopy(baseline)
    changed['evaluation']['whole']['fields']['raw_value']['recall'] = .5
    with pytest.raises(ValueError, match='digest'):
        module.evaluate_release_snapshot(dictionary, baseline_report=changed)


def test_actual_extraction_runs_once_per_fixed_case(dictionary, monkeypatch):
    from apps.labs import extraction
    module = evaluator()
    original = extraction.extract_observations
    calls = []

    def observe(pages, dictionary):
        calls.append(tuple((page.page_number, tuple(region.text for region in page.regions)) for page in pages))
        return original(pages, dictionary)

    monkeypatch.setattr(extraction, 'extract_observations', observe)
    report = module.evaluate_release_snapshot(dictionary)
    assert len(calls) == report['evaluation']['whole']['files']['total']


def test_historical_predictions_do_not_inherit_current_downstream_or_missing_evidence(monkeypatch):
    module = evaluator()
    case = module.frozen_release_cases()[0]
    row = {key: value for key, value in case['expected'][0].items()
           if key in {'raw_name', 'standard_code', 'raw_value', 'raw_unit', 'result_type', 'page_number'}}
    monkeypatch.setattr(module, 'comparable_cell', lambda *args, **kwargs: pytest.fail('Historical scoring ran current downstream'))
    scored = module.score_frozen_predictions([{'source_file_hash': case['source_file_hash'],
        'status': 'success', 'observations': [row]}])
    assert scored['whole']['files']['total'] == len(module.frozen_release_cases())
    assert scored['whole']['fields']['raw_value']['true_positive'] == 1
    assert scored['whole']['trend'] == {'assessed': 0, 'eligible': 0, 'eligible_ratio': None}
    assert scored['whole']['localization']['correct_fields'] == 0
    assert scored['output_availability']['field_evidence']['missing_rows'] == 1
    assert scored['output_availability']['specimen']['missing_rows'] == 1
    assert scored['downstream']['assessed'] is False
    assert scored['normal']['routing_rate'] is None
    assert scored['known_error_interception']['recall'] is None


def test_downstream_rule_cannot_pass_while_routing_frozen_normal_controls(dictionary):
    broken_conversion = history_rule(kind='conversion', method='SYNTHETIC-FIXED-METHOD',
        source_unit='g/L', target_unit='g/L', factor='0')
    report = evaluator().evaluate_release_snapshot(dictionary, [broken_conversion])
    assert report['counts']['normal']['preserved'] == 98  # The original extraction gate still succeeds.
    assert report['evaluation']['normal']['routed'] > 0
    assert not report['passed']
    assert any(item['reason'] == 'downstream_normal_control_routed' for item in report['failures'])


def test_extra_context_extraction_failure_blocks_phase_two_release(dictionary, monkeypatch):
    from apps.labs import extraction
    module = evaluator()
    original = extraction.extract_observations

    def break_extra(pages, dictionary):
        texts = [region.text for page in pages for region in page.regions]
        if '1' in texts and 'g/dL' in texts:
            return ()
        return original(pages, dictionary)

    monkeypatch.setattr(extraction, 'extract_observations', break_extra)
    report = module.evaluate_release_snapshot(dictionary)
    assert report['evaluation']['whole']['joint']['false_negative'] >= 1
    assert not report['passed']
    assert any(item['reason'] == 'release_context_transcription_failed' for item in report['failures'])


def test_fixed_conversion_and_report_sum_contexts_exercise_production_rules(dictionary):
    rules = [history_rule(id='synthetic-conversion', kind='conversion', source_unit='g/dL', target_unit='g/L', factor='10'),
             history_rule(id='synthetic-sum', kind='report_sum', code='LAB_TBIL', unit='umol/L',
                          component_codes=['LAB_DBIL', 'LAB_IBIL'], absolute_tolerance='0')]
    report = evaluator().evaluate_release_snapshot(dictionary, rules)
    assert report['passed'], report['failures']
    coverage = {item['rule_id']: item for item in report['rule_coverage']}
    assert coverage['synthetic-conversion']['applicable'] == coverage['synthetic-conversion']['triggered'] == 1
    assert coverage['synthetic-sum']['applicable'] == 2
    assert coverage['synthetic-sum']['triggered'] == 1
    output, = report['evaluation']['conversion_outputs']
    assert (output['case_id'], output['value'], output['unit']) == ('release:conversion', '10', 'g/L')


def test_current_parser_baseline_binds_both_dictionary_and_rule_snapshot(dictionary):
    rule = history_rule(method='UNSEEN-METHOD')
    report = evaluator().evaluate_release_snapshot(dictionary, baseline=dictionary, baseline_rules=[rule])
    baseline = report['identity']['current_parser_baseline']
    assert baseline['dictionary_sha256'] == dictionary.content_hash
    assert baseline['rules_sha256'] == rules_digest([rule])
    assert report['evaluation']['current_parser_baseline']['whole']['annotated_rows'] == 564


def test_exact_legacy_release_keeps_gate_scope_but_scores_full_frozen_corpus():
    legacy = load_dictionary(ROOT / 'apps/labs/dictionaries/v1.0.0.json')
    baseline = json.loads((ROOT / 'apps/labs/dictionaries/phase-two-baseline.json').read_text(encoding='utf-8'))
    report = evaluator().evaluate_release_snapshot(legacy, baseline_report=baseline)
    assert report['passed'], report['failures']
    assert report['scope'] == 'legacy'
    assert report['counts']['target_positive']['assessed'] == 0
    assert report['evaluation']['whole']['files']['total'] == 556
    assert report['evaluation']['whole']['annotated_rows'] == 564
    assert report['evaluation']['control_gate']['enforced'] is False
    assert report['evaluation']['whole']['joint']['recall'] < 1


def test_corpus_identity_change_during_execution_is_rejected(dictionary, monkeypatch):
    module = evaluator()
    original = module._dataset
    calls = 0

    def changing_dataset():
        nonlocal calls
        calls += 1
        fixed, context, hashes = original()
        return fixed, context, {**hashes, **({'release_context': 'f' * 64} if calls > 2 else {})}

    monkeypatch.setattr(module, '_dataset', changing_dataset)
    with pytest.raises(ValueError, match='corpus changed'):
        module.evaluate_release_snapshot(dictionary)
