from copy import deepcopy
import hashlib
import json

import pytest


def inputs():
    annotations = {'schema_version': 1, 'review_status': 'adjudicated', 'rows': [{
        'annotation_id': 'synthetic-a', 'source_file_hash': 'a' * 64, 'report_group_id': 'g1', 'page_number': 1,
        'row_index': 1, 'raw_name': 'Synthetic A', 'standard_code': 'LAB_A', 'raw_value': '5.2',
        'result_type': 'numeric', 'raw_unit': 'g/L', 'reference_range_raw': '3-8', 'report_date': '2026-08-20',
        'specimen': 'BLOOD', 'source_verified': True, 'field_sources': {}, 'unknown_reasons': {},
    }], 'reports': [{'report_group_id': 'g1', 'target_rows_complete': True}]}
    row = {'page_number': 1, 'reading_order': 1, 'raw_name': 'Synthetic A', 'standard_code': 'LAB_A',
           'raw_value': '5.2', 'result_type': 'NUMERIC', 'raw_unit': 'g/L', 'reference_range_raw': '3-8',
           'observation_date': '2026-08-20', 'specimen': 'BLOOD', 'capability_level': 'STABLE',
           'confidence': .99, 'quality_issues': [], 'trend_eligible': True, 'field_evidence': {}}
    predictions = [{'source_file_hash': 'a' * 64, 'status': 'success', 'observations': [row]},
                   {'source_file_hash': 'b' * 64, 'status': 'original_only', 'observations': []}]
    classification = {'files': [{'source_file_hash': 'a' * 64, 'document_type': 'laboratory', 'report_group_ids': ['g1']},
                                {'source_file_hash': 'b' * 64, 'document_type': 'imaging', 'report_group_ids': ['g2']}]}
    return predictions, annotations, classification


def test_evaluation_uses_full_file_denominator_and_counts_duplicate_predictions():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    predictions[0]['observations'].append(deepcopy(predictions[0]['observations'][0]))
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['files'] == {'total': 2, 'success': 1, 'original_only': 1, 'failed': 0, 'missing': 0}
    assert report['joint']['true_positive'] == 1
    assert report['joint']['false_positive'] == 1
    assert report['joint']['false_negative'] == 0
    assert report['joint']['precision'] == .5


def test_failed_extraction_remains_in_recall_denominator():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    predictions[0].update(status='failed', observations=[])
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['joint']['false_negative'] == 1
    assert report['joint']['recall'] == 0
    assert report['successful_subset']['annotated_rows'] == 0
    assert report['fields']['raw_value']['false_negative'] == 1


def test_comparator_loss_counts_as_severe_even_when_intercepted():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    annotations['rows'][0].update(raw_value='<5.2', result_type='comparator')
    predictions[0]['observations'][0].update(capability_level='SEARCH_ONLY', trend_eligible=False,
        quality_issues=[{'code': 'association_conflict'}])
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['severe']['errors'] == 1
    assert report['severe']['intercepted'] == 1
    assert report['severe']['unintercepted'] == 0
    assert report['joint']['true_positive'] == 0


def test_unknown_fields_and_unannotated_scopes_are_not_falsely_counted_as_perfect():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    annotations['rows'][0]['reference_range_raw'] = None
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['fields']['reference_range_raw']['recall'] is None
    assert report['localization']['exact_rate'] is None
    assert report['localization']['eligible_fields'] == 0
    annotations['rows'] = []
    assert evaluate_predictions(predictions, annotations, classification)['joint']['f1'] is None


def test_gate_requires_adjudication_and_no_recall_regression():
    from tools.phase_two_evaluation import evaluate_predictions, regression_gate
    predictions, annotations, classification = inputs()
    baseline = evaluate_predictions(predictions, annotations, classification)
    annotations['review_status'] = 'primary_only'
    primary = evaluate_predictions(predictions, annotations, classification)
    assert regression_gate(primary, baseline)['passed'] is False
    annotations['review_status'] = 'adjudicated'
    predictions[0]['observations'] = []
    current = evaluate_predictions(predictions, annotations, classification)
    assert regression_gate(current, baseline)['passed'] is False


def test_evaluation_rejects_duplicate_or_foreign_source_files():
    from tools.phase_two_evaluation import evaluate_predictions, EvaluationError
    predictions, annotations, classification = inputs()
    with pytest.raises(EvaluationError):
        evaluate_predictions([*predictions, predictions[0]], annotations, classification)
    predictions[0]['source_file_hash'] = 'c' * 64
    with pytest.raises(EvaluationError):
        evaluate_predictions(predictions, annotations, classification)


def test_equal_value_swapped_project_codes_remain_two_mapping_errors():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    second = deepcopy(annotations['rows'][0])
    second.update(annotation_id='synthetic-b', raw_name='Synthetic B', standard_code='LAB_B', row_index=2)
    annotations['rows'].append(second)
    original = predictions[0]['observations'][0]
    other = deepcopy(original)
    original['standard_code'] = 'LAB_B'
    other.update(raw_name='Synthetic B', standard_code='LAB_A', reading_order=2)
    predictions[0]['observations'].append(other)
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['fields']['standard_code']['true_positive'] == 0
    assert report['joint']['true_positive'] == 0
    assert report['severe']['errors'] == 2


def test_partial_annotation_does_not_call_unannotated_prediction_a_severe_error():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    annotations['reports'][0]['target_rows_complete'] = False
    extra = deepcopy(predictions[0]['observations'][0])
    extra.update(raw_name='Synthetic B', standard_code='LAB_B', reading_order=2)
    predictions[0]['observations'].append(extra)
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['joint']['false_positive'] == 0
    assert report['severe']['errors'] == 0
    assert report['unassessed_prediction_rows'] == 1


def test_no_verified_severe_fields_has_no_severity_denominator():
    from tools.phase_two_evaluation import evaluate_predictions, SEVERE_FIELDS
    predictions, annotations, classification = inputs()
    for field in SEVERE_FIELDS:
        annotations['rows'][0][field] = None
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['severe']['assessed_predictions'] == 0
    assert report['severe']['error_rate'] is None


def test_zero_row_incomplete_group_prevents_claiming_a_mixed_page_is_complete():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    classification['files'][0]['report_group_ids'].append('g3')
    annotations['reports'].append({'report_group_id': 'g3', 'target_rows_complete': False})
    extra = deepcopy(predictions[0]['observations'][0])
    extra.update(raw_name='Synthetic C', standard_code='LAB_C', reading_order=2)
    predictions[0]['observations'].append(extra)
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['joint']['false_positive'] == 0
    assert report['severe']['errors'] == 0
    assert report['unassessed_prediction_rows'] == 1


def test_tier_false_positive_is_not_removed_by_empty_tier_gold():
    from tools.phase_two_evaluation import quality_breakdowns
    predictions, annotations, classification = inputs()
    extra = deepcopy(predictions[0]['observations'][0])
    extra.update(raw_name='Synthetic B', standard_code='LAB_B', reading_order=2)
    predictions[0]['observations'].append(extra)
    coverage = {'target_definitions': [{'code': 'LAB_A', 'tier': 'TIER_1'}, {'code': 'LAB_B', 'tier': 'TIER_2'}]}
    report = quality_breakdowns(predictions, annotations, classification, coverage)
    assert report['tiers']['TIER_2']['joint']['false_positive'] == 1


def test_extra_target_prediction_prevents_whole_dual_report_success():
    from tools.phase_two_evaluation import quality_breakdowns
    predictions, annotations, classification = inputs()
    classification['reports'] = [{**annotations['reports'][0], 'layout': 'dual_column'}]
    extra = deepcopy(predictions[0]['observations'][0])
    extra.update(raw_value='7', reading_order=2)
    predictions[0]['observations'].append(extra)
    coverage = {'target_definitions': [{'code': 'LAB_A', 'tier': 'TIER_1'}]}
    report = quality_breakdowns(predictions, annotations, classification, coverage)
    assert report['dual_column']['complete_target_association_groups'] == 0


def test_unit_exponent_is_not_erased_by_evaluation_normalization():
    from tools.phase_two_evaluation import _equal
    assert _equal('raw_unit', '10⁹/L', '10^9/L')
    assert not _equal('raw_unit', '10⁹/L', '109/L')
    assert not _equal('raw_unit', '10^12/L', '10^9/L')


def test_baseline_document_date_is_evaluated_as_it_was_persisted():
    from tools.phase_two_evaluation import evaluate_predictions
    predictions, annotations, classification = inputs()
    predictions[0]['observations'][0].pop('observation_date')
    predictions[0]['metadata'] = {'document_date': '2026-08-20'}
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['fields']['report_date']['recall'] == 1
    # An explicit unresolved row date must not borrow the outer document date.
    predictions[0]['observations'][0]['observation_date'] = None
    report = evaluate_predictions(predictions, annotations, classification)
    assert report['fields']['report_date']['recall'] == 0


def test_frozen_cache_rejects_modified_source_or_ocr_bytes(tmp_path):
    from tools.phase_two_evaluation import load_frozen_pages, EvaluationError
    source = tmp_path / 'source.bin'
    source.write_bytes(b'fixed synthetic original')
    identity = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = tmp_path / f'{identity}.json'
    cache.write_text(json.dumps({'source_file_hash': identity, 'pages': []}), encoding='utf-8')
    manifest = {'source_file_hash': identity, 'ocr_cache_sha256': hashlib.sha256(cache.read_bytes()).hexdigest(), 'ocr_pages': 0}
    row = {'hash': identity, 'path': str(source)}
    assert load_frozen_pages(row, tmp_path, manifest) == ()
    cache.write_text(cache.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    with pytest.raises(EvaluationError, match='cache'):
        load_frozen_pages(row, tmp_path, manifest)
    source.write_bytes(b'changed source')
    with pytest.raises(EvaluationError, match='source'):
        load_frozen_pages(row, tmp_path, manifest)


@pytest.mark.django_db(transaction=True)
def test_cached_evaluation_uses_real_persistence_quality_and_comparison(tmp_path):
    from dataclasses import asdict
    from pathlib import Path
    from apps.labs.dictionary import load_dictionary
    from tools.phase_two_evaluation import predict_frozen_sources
    from tests.labs.test_phase_two_layout import page

    source = tmp_path / 'source.bin'
    source.write_bytes(b'fixed synthetic source with an unreviewed calcium unit')
    identity = hashlib.sha256(source.read_bytes()).hexdigest()
    ocr_page = page([(.03, [(.05, '标本：全血')]), (.07, [(.05, '采样日期：2026-08-20')]),
        (.12, [(.05, '项目'), (.3, '结果'), (.5, '单位'), (.7, '参考范围'), (.87, '方法')]),
        (.2, [(.05, '钙'), (.3, '1'), (.5, 'synthetic/L'), (.7, '0-2'), (.87, '方法甲')])])
    encoded_page = asdict(ocr_page)
    encoded_page['provider_metadata'] = dict(ocr_page.provider_metadata)
    cache = tmp_path / f'{identity}.json'
    cache.write_text(json.dumps({'source_file_hash': identity, 'pages': [encoded_page]}), encoding='utf-8')
    manifest = {'samples': [{'source_file_hash': identity, 'ocr_cache_sha256': hashlib.sha256(cache.read_bytes()).hexdigest(), 'ocr_pages': 1}]}
    classification = {'files': [{'source_file_hash': identity, 'patient_group_id': 'synthetic-group'}]}
    result = predict_frozen_sources([{'hash': identity, 'path': str(source)}], tmp_path, manifest,
        classification, load_dictionary(Path('apps/labs/dictionaries/phase-two.json')))
    record, = result['predictions']
    observation, = record['observations']
    assert record['status'] == 'success'
    assert observation['observation_date'] == '2026-08-20'
    assert observation['trend_eligible'] is False
    assert 'unit_unknown' in {item['code'] for item in observation['quality_issues']}
    assert observation['field_evidence']['observation_date']['page_number'] == 1
    assert result['execution']['persistence_and_comparison'] is True


@pytest.mark.django_db(transaction=True)
def test_unpublished_dictionary_and_rules_resolve_only_inside_replay_database(tmp_path):
    from apps.labs.dictionary import load_dictionary, dictionary_for_version, rules_for_version
    from tools.phase_two_evaluation import register_evaluation_snapshot, EvaluationError
    from django.db import connection
    from pathlib import Path
    payload = json.loads(Path('apps/labs/dictionaries/phase-two.json').read_text(encoding='utf-8'))
    payload['dictionary_version'] = 'synthetic-unpublished'
    external = tmp_path / 'dictionary.json'
    external.write_text(json.dumps(payload), encoding='utf-8')
    dictionary = load_dictionary(external)
    rules = [{'id': 'synthetic-rule', 'version': '1', 'kind': 'conversion'}]
    snapshot = register_evaluation_snapshot(dictionary, rules)
    assert dictionary_for_version(snapshot.version).content_hash == snapshot.content_hash
    assert list(rules_for_version(snapshot.version)) == rules
    original_name = connection.settings_dict['NAME']
    try:
        connection.settings_dict['NAME'] = 'persistent.sqlite3'
        with pytest.raises(EvaluationError, match='in-memory'):
            register_evaluation_snapshot(dictionary, rules)
    finally:
        connection.settings_dict['NAME'] = original_name
