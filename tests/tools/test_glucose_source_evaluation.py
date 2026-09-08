"""Synthetic source-fidelity contracts; no real gold, OCR or application imports."""
from copy import deepcopy
import hashlib
import json

import pytest

from tools.glucose_source_evaluation import score_predictions


METRICS = [
    'value', 'raw_value_retained', 'unit', 'raw_unit_retained', 'source_type', 'printed_specimen',
    'sampling_local_datetime', 'sampling_raw_time_retained', 'sampling_role', 'sampling_precision',
    'reporting_local_datetime', 'reporting_raw_time_retained', 'reporting_role', 'reporting_precision',
    'unconfirmed_timezone_preserved', 'utc_not_invented', 'meal_context', 'meal_context_basis',
]


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def source(number):
    return {'source_sha256': sha(f'synthetic-original-{number}'),
            'fixed_ocr_sha256': sha(f'synthetic-ocr-{number}'), 'manifest_page': 1}


def time_value(raw, role):
    return {'raw': raw, 'local_datetime': raw, 'precision': 'SECOND', 'role': role,
            'printed_timezone': None, 'printed_utc_offset': None, 'timezone_status': 'UNCONFIRMED',
            'timezone_name': None, 'utc_offset': None, 'utc_datetime': None}


def bundle(*, fasting=True, value_block='5.50'):
    """Hand-authored one-row/one-negative-page miniature of the frozen structure."""
    input_source = source(1)
    expected = {
        'source_type': 'LABORATORY_REPORT', 'specimen_raw': '血清', 'decimal_value': '5.50',
        'raw_value': '5.50', 'unit': 'mmol/L', 'raw_unit': 'mmol/L',
        'sampling': time_value('2024-04-01 07:08:09', 'SPECIMEN_SAMPLING'),
        'reporting': time_value('2024-04-01 10:11:12', 'LAB_REPORT_ISSUANCE'),
        'meal_context': {'status': 'FASTING' if fasting else 'NOT_STATED',
                         'basis': 'PRINTED_REQUESTED_TEST_HEADING' if fasting else 'NO_EXPLICIT_MEAL_CONDITION_ON_ORIGINAL_PANEL',
                         'actual_preparation_independently_verified': False, 'clock_inference_allowed': False},
    }
    evidence, proofs, input_blocks = [], {}, []

    def add(key, text, wanted, index, y, *, where=input_source):
        start = text.index(wanted)
        polygon = [[.1, y], [.4, y], [.4, y + .02], [.1, y + .02]]
        fragment = {'region_array_index': index, 'block_reading_order': index,
                    'char_start': start, 'char_end_exclusive': start + len(wanted),
                    'raw_fixed_ocr_fragment': wanted, 'full_block_text_sha256': sha(text),
                    'original_normalized_polygon': polygon}
        evidence.append({'evidence_id': key, **where, 'fixed_ocr_fragments': [fragment]})
        claim = {**where, **deepcopy(fragment), 'block_text': text}
        proofs[key] = [claim]
        input_blocks.append({**where, 'region_array_index': index, 'block_reading_order': index,
                             'block_text': text, 'original_normalized_polygon': polygon})

    add('item_code', 'GLU', 'GLU', 0, .1)
    add('item_name', '葡萄糖', '葡萄糖', 1, .15)
    add('value', value_block, '5.50', 2, .2)
    add('unit', 'mmol/L', 'mmol/L', 3, .25)
    add('specimen', '标本：血清', '血清', 4, .3)
    add('sampling_label', '采样时间：2024-04-01 07:08:09', '采样时间', 5, .4)
    add('sampling_time', '采样时间：2024-04-01 07:08:09', '2024-04-01 07:08:09', 5, .4)
    add('reporting_label', '报告时间：2024-04-01 10:11:12', '报告时间', 6, .5)
    add('reporting_time', '报告时间：2024-04-01 10:11:12', '2024-04-01 10:11:12', 6, .5)
    if fasting:
        add('meal_context', '检验项目：空腹血糖', '检验项目：空腹血糖', 7, .05)
    refs = {key: key for key in proofs}
    refs['meal_context'] = 'meal_context' if fasting else None
    row = {'gold_id': 'row-A', 'source': input_source, 'panel_id': 'panel-A',
           'panel_scope': {'original_normalized_bbox': [0, 0, 1, 1]}, 'expected': expected,
           'field_evidence_refs': refs, 'negative_assertions_from_original_review': {
               'no_printed_timezone_or_offset_on_this_panel': True,
               'no_meal_condition_on_this_panel': not fasting}}
    candidate = {'prediction_id': 'persisted-A', 'disposition': 'ADMITTED', 'source': deepcopy(input_source),
                 'values': deepcopy(expected), 'row_evidence': {k: deepcopy(proofs[k]) for k in ('value', 'item_code', 'item_name')},
                 'field_evidence': deepcopy(proofs)}
    add('excluded-range', '近期血糖5.0–8.0mmol/L', '5.0–8.0', 0, .2, where=source(2))
    negative = {'negative_page_id': 'negative-A', 'source': source(2), 'case_ids': ['case-A']}
    gold = {'schema_version': 1, 'positive_rows': [row], 'negative_pages': [negative],
            'negative_cases': [{'case_id': 'case-A', 'negative_page_id': 'negative-A', 'evidence_refs': ['excluded-range']}],
            'unrepresented_positive_categories': {'date_only': 0, 'missing_unit': 0, 'individual_capillary_or_nursing_table': 0,
                                                  'printed_timezone': 0, 'mg_per_dl': 0}}
    coverage = {'pages': [{**source(i), 'source_number': i, 'status': status} for i, status in enumerate([
        'POSITIVE_REVIEWED_PANEL', 'NEGATIVE_ADMISSION_BOUNDARY_PAGE',
        'PREVIOUSLY_REVIEWED_BUT_OUT_OF_SCOPE', 'UNJUDGED_NOT_ORIGINAL_REVIEWED'], 1)]}
    predictions = {'schema_version': 1, 'pages': [{
        'source': source(i), 'status': 'COMPLETE', 'items': [candidate] if i == 1 else [],
        'input_blocks': [b for b in input_blocks if b['source_sha256'] == source(i)['source_sha256']],
    } for i in range(1, 5)]}
    protocol = {'schema_version': 1, 'per_field_metrics': {'denominator_each': 1, 'metrics': [{'id': k} for k in METRICS]}}
    return gold, protocol, {'evidence': evidence}, coverage, predictions


def score(data):
    return score_predictions(*data)


def item(data):
    return data[4]['pages'][0]['items'][0]


def field(result, metric):
    return result['summary']['fields'][metric]


def negative_item(data, *, disposition='ADMITTED'):
    proof = next(e for e in data[2]['evidence'] if e['evidence_id'] == 'excluded-range')
    block = data[4]['pages'][1]['input_blocks'][0]
    return {'prediction_id': 'negative-item', 'disposition': disposition,
            'source': source(2), 'values': {},
            'diagnostic_evidence': [{**source(2), **proof['fixed_ocr_fragments'][0], 'block_text': block['block_text']}]}


def test_each_of_eighteen_fields_has_its_own_complete_denominator_without_an_aggregate_accuracy():
    result = score(bundle())
    assert set(result['summary']['fields']) == set(METRICS)
    for metric in METRICS:
        assert field(result, metric)['CORRECT'] == 1
        assert sum(field(result, metric)[k] for k in ('CORRECT', 'MISMATCH', 'MISSING', 'SOURCE_UNVERIFIED')) == 1
    assert result['summary']['source_rows'] == {'denominator': 1, 'recovered': 1, 'missing': 0}
    assert 'accuracy' not in result['summary'] and 'overall_precision' not in result['summary']


@pytest.mark.parametrize('raw,correct', [('5.500', True), ('５．５０', True), ('5.5001', False),
                                        ('5.50–8.0', False), ('<5.50', False), ('NaN', False), ('Infinity', False)])
def test_decimal_value_is_exact_and_never_repaired_from_a_range_or_nonfinite_token(raw, correct):
    data = bundle(); item(data)['values']['decimal_value'] = raw
    assert field(score(data), 'value')['CORRECT' if correct else 'MISMATCH'] == 1


def test_equal_decimal_formatting_does_not_erase_a_raw_value_retention_failure():
    data = bundle(); item(data)['values'].update(decimal_value='5.5', raw_value='5.5')
    result = score(data)
    assert field(result, 'value')['CORRECT'] == 1
    assert field(result, 'raw_value_retained')['MISMATCH'] == 1


@pytest.mark.parametrize('unit,correct', [('mmol/l', True), ('ｍｍｏｌ／Ｌ', True), ('mmol / L', True),
                                       ('mmo1/L', False), ('mg/dL', False), ('%', False)])
def test_unit_normalization_never_changes_one_to_letter_l_or_converts_a_unit(unit, correct):
    data = bundle(); item(data)['values']['unit'] = unit
    assert field(score(data), 'unit')['CORRECT' if correct else 'MISMATCH'] == 1


def test_semantic_unit_equivalence_does_not_rewrite_printed_raw_unit_casing():
    data = bundle(); item(data)['values'].update(unit='mmol/l', raw_unit='mmol/l')
    result = score(data)
    assert field(result, 'unit')['CORRECT'] == 1
    assert field(result, 'raw_unit_retained')['MISMATCH'] == 1


def test_wrong_value_takes_mismatch_precedence_over_missing_field_proof():
    data = bundle(); item(data)['values']['unit'] = 'mmo1/L'; item(data)['field_evidence'].pop('unit')
    assert field(score(data), 'unit')['MISMATCH'] == 1


def test_correct_values_from_wrong_own_field_or_time_role_are_source_unverified():
    data = bundle(); current = item(data)
    current['field_evidence']['unit'] = deepcopy(current['field_evidence']['value'])
    current['field_evidence']['sampling_time'] = deepcopy(current['field_evidence']['reporting_time'])
    result = score(data)
    assert field(result, 'unit')['SOURCE_UNVERIFIED'] == 1
    assert field(result, 'sampling_local_datetime')['SOURCE_UNVERIFIED'] == 1
    assert result['summary']['source_rows']['recovered'] == 1


def test_missing_null_and_unsupported_timezone_are_distinct():
    data = bundle(); current = item(data)
    current['values']['sampling'].pop('utc_datetime')
    current['values']['reporting']['timezone_name'] = 'Asia/Shanghai'
    result = score(data)
    assert field(result, 'utc_not_invented')['MISSING'] == 1
    assert field(result, 'unconfirmed_timezone_preserved')['MISMATCH'] == 1
    assert field(result, 'sampling_local_datetime')['CORRECT'] == 1


def test_partial_or_report_substituted_time_does_not_pass_exact_sampling_seconds():
    data = bundle(); current = item(data)
    current['values']['sampling'].update(local_datetime='2024-04-01T10:11:12', raw='2024-04-01 07:08', precision='MINUTE')
    result = score(data)
    for metric in ('sampling_local_datetime', 'sampling_raw_time_retained', 'sampling_precision'):
        assert field(result, metric)['MISMATCH'] == 1


def test_explicit_abstention_stays_in_the_fixed_denominator():
    data = bundle(); item(data)['abstained_fields'] = ['unit']
    result = score(data)
    assert field(result, 'unit')['MISSING'] == 1
    assert field(result, 'unit')['denominator'] == 1


def test_known_not_stated_and_no_timezone_are_scored_against_reviewed_absence():
    result = score(bundle(fasting=False))
    assert field(result, 'meal_context')['CORRECT'] == 1
    assert field(result, 'unconfirmed_timezone_preserved')['CORRECT'] == 1
    assert result['summary']['meal_strata']['NOT_STATED']['denominator'] == 1
    assert result['summary']['meal_strata']['FASTING']['denominator'] == 0


def test_constant_fasting_and_independent_preparation_claim_fail_an_ordinary_panel():
    data = bundle(fasting=False)
    item(data)['values']['meal_context'].update(status='FASTING', actual_preparation_independently_verified=True)
    result = score(data)
    assert field(result, 'meal_context')['MISMATCH'] == 1
    assert field(result, 'meal_context_basis')['MISMATCH'] == 1


@pytest.mark.parametrize('ids', [('a', 'b'), ('', ''), (None, None), ('same', 'same')])
def test_duplicate_pairing_never_selects_the_candidate_with_the_best_values(ids):
    data = bundle(); first = item(data); second = deepcopy(first)
    first['prediction_id'], second['prediction_id'] = ids
    first['values']['decimal_value'] = '9.00'
    data[4]['pages'][0]['items'] = [first, second]
    result = score(data)
    assert field(result, 'value')['MISMATCH'] == 1
    assert result['summary']['positive_admissions']['duplicates'] == 1
    assert result['details']['rows'][0]['output_index'] == 0


def test_lexicographic_id_pairing_is_stable_even_when_output_array_order_changes():
    data = bundle(); first = item(data); second = deepcopy(first)
    first.update(prediction_id='z'); second.update(prediction_id='a')
    second['values']['decimal_value'] = '9.00'
    data[4]['pages'][0]['items'].append(second)
    result = score(data)
    assert field(result, 'value')['MISMATCH'] == 1
    assert result['details']['rows'][0]['prediction_id'] == 'a'


def test_admitted_without_row_location_remains_an_extra_and_keeps_the_gold_missing():
    data = bundle(); item(data).pop('row_evidence')
    result = score(data)
    assert result['summary']['source_rows']['missing'] == 1
    assert result['summary']['positive_admissions']['unverified_row_extras'] == 1
    assert all(field(result, metric)['MISSING'] == 1 for metric in METRICS)


def test_a_cross_page_item_cannot_change_its_trusted_input_envelope_to_recover_a_row():
    data = bundle(); candidate = data[4]['pages'][0]['items'].pop()
    data[4]['pages'][1]['items'].append(candidate)
    result = score(data)
    assert result['summary']['source_rows']['recovered'] == 0
    assert result['summary']['negative_pages']['false_admitted_items'] == 1
    assert result['summary']['unattributable_admissions'] == 1


@pytest.mark.parametrize('status', ['FAILED', 'NOT_RUN'])
def test_failed_negative_page_is_not_a_successfully_rejected_page(status):
    data = bundle(); data[4]['pages'][1]['status'] = status
    result = score(data)
    assert result['summary']['negative_pages']['completed_without_admission'] == 0
    assert result['summary']['negative_pages']['unassessed_execution'] == 1
    assert result['details']['negative_cases'][0]['status'] == 'UNASSESSED_EXECUTION'


def test_failed_negative_page_retains_admissions_even_without_case_or_row_location():
    data = bundle(); page = data[4]['pages'][1]
    page.update(status='FAILED', items=[{'disposition': 'ADMITTED', 'prediction_id': ''}])
    result = score(data)
    assert result['summary']['negative_pages']['false_admission_pages'] == 1
    assert result['summary']['negative_pages']['false_admitted_items'] == 1
    assert result['summary']['negative_pages']['unmatched_case_items'] == 1
    assert result['summary']['negative_pages']['completed_without_admission'] == 0


def test_negative_diagnostic_groups_are_not_an_allowlist_and_safe_exclusion_is_separate():
    data = bundle(); page = data[4]['pages'][1]
    page['items'] = [negative_item(data, disposition='EXCLUDED')]
    result = score(data)
    assert result['details']['negative_cases'][0]['status'] == 'SAFE_EXCLUSION'
    assert result['summary']['negative_pages']['false_admitted_items'] == 0
    page['items'].append(negative_item(data))
    result = score(data)
    assert result['details']['negative_cases'][0]['status'] == 'FALSE_ADMISSION'
    assert result['summary']['negative_cases']['denominator'] == 1
    assert result['summary']['negative_pages']['false_admission_pages'] == 1


def test_unreviewed_and_previously_reviewed_unselected_outputs_get_no_true_false_scores():
    data = bundle()
    for page in data[4]['pages'][2:]:
        page['items'] = [{'disposition': 'ADMITTED', 'prediction_id': 'private-outside'}]
    result = score(data)
    assert result['summary']['out_of_scope']['unreviewed_admissions'] == 1
    assert result['summary']['out_of_scope']['previously_reviewed_admissions'] == 1
    assert result['summary']['negative_pages']['false_admitted_items'] == 0
    assert result['summary']['source_rows']['denominator'] == 1


def test_an_omitted_input_page_stays_unprocessed_in_the_fixed_coverage():
    data = bundle(); data[4]['pages'].pop(1)
    result = score(data)
    assert result['summary']['execution']['fixed_pages'] == 4
    assert result['summary']['execution']['complete_pages'] == 3
    assert result['summary']['negative_pages']['unassessed_execution'] == 1


def test_cyclic_equivalent_original_polygon_can_prove_a_fixed_block_but_a_union_cannot():
    data = bundle(); claim = item(data)['field_evidence']['unit'][0]
    claim.pop('region_array_index'); claim.pop('block_reading_order')
    polygon = claim['original_normalized_polygon']
    claim['original_normalized_polygon'] = polygon[1:] + polygon[:1]
    assert field(score(data), 'unit')['CORRECT'] == 1
    claim['original_normalized_polygon'] = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert field(score(data), 'unit')['SOURCE_UNVERIFIED'] == 1


@pytest.mark.parametrize('text,correct', [('结果 5.50 空白', True), ('5.50 / 5.50', False)])
def test_a_larger_claimed_quote_requires_a_unique_needed_substring_in_the_same_block(text, correct):
    data = bundle(value_block=text); claim = item(data)['field_evidence']['value'][0]
    claim['char_start'], claim['char_end_exclusive'] = 0, len(text)
    assert field(score(data), 'value')['CORRECT' if correct else 'SOURCE_UNVERIFIED'] == 1


def test_false_field_hash_or_wrong_original_offset_never_proves_a_correct_semantic_value():
    data = bundle(); claim = item(data)['field_evidence']['sampling_time'][0]
    claim['char_start'] = 0; claim['char_end_exclusive'] = 2
    assert field(score(data), 'sampling_local_datetime')['SOURCE_UNVERIFIED'] == 1
    claim['full_block_text_sha256'] = '0' * 64
    assert field(score(data), 'sampling_local_datetime')['SOURCE_UNVERIFIED'] == 1


def test_public_summary_contains_no_original_values_or_private_item_ids_and_inputs_are_immutable():
    data = bundle(); saved = deepcopy(data); result = score(data)
    public = json.dumps(result['summary'], ensure_ascii=False)
    assert '5.50' not in public and '2024-04-01' not in public and 'persisted-A' not in public
    assert data == saved
    assert all(v['denominator'] == 0 and v['status'] == 'NOT_EVALUATED'
               for v in result['summary']['unrepresented_positive_categories'].values())


def test_a_null_input_source_preserves_its_admission_as_unattributable_without_crashing():
    data = bundle(); page = data[4]['pages'][1]
    page.update(source=None, items=[{'disposition': 'ADMITTED'}])
    result = score(data)
    assert result['summary']['unattributable_admissions'] == 1
    assert result['summary']['execution']['not_run_pages'] == 1
    assert result['summary']['negative_pages']['completed_without_admission'] == 0


def test_wrong_ocr_hash_on_a_known_negative_source_does_not_hide_its_admissions():
    data = bundle(); page = data[4]['pages'][1]
    page['source']['fixed_ocr_sha256'] = sha('different OCR')
    page['items'] = [{'disposition': 'ADMITTED'}]
    result = score(data)
    assert result['summary']['negative_pages']['false_admitted_items'] == 1
    assert result['summary']['negative_pages']['unassessed_execution'] == 1
    assert result['summary']['execution']['invalid_envelope_pages'] == 1


def test_repeated_page_envelopes_do_not_turn_an_ambiguous_execution_into_a_successful_rejection():
    data = bundle(); data[4]['pages'].append(deepcopy(data[4]['pages'][1]))
    result = score(data)
    assert result['summary']['negative_pages']['unassessed_execution'] == 1
    assert result['summary']['negative_pages']['completed_without_admission'] == 0


def test_an_output_polygon_without_a_trusted_input_block_cannot_escape_to_an_unselected_panel():
    data = bundle(); data[0]['positive_rows'][0]['panel_scope']['original_normalized_bbox'] = [0, 0, .5, 1]
    current = item(data); current.pop('row_evidence')
    current['source_polygon'] = [[.7, .1], [.9, .1], [.9, .2], [.7, .2]]
    result = score(data)
    assert result['summary']['positive_admissions']['unverified_row_extras'] == 1
    assert result['summary']['out_of_scope']['unselected_panel_admissions'] == 0


def test_only_actual_outside_panel_blocks_can_prove_an_unselected_panel():
    data = bundle(); data[0]['positive_rows'][0]['panel_scope']['original_normalized_bbox'] = [0, 0, .5, 1]
    current = item(data)
    for role, claims in current['row_evidence'].items():
        for claim in claims:
            claim['region_array_index'] += 20; claim['block_reading_order'] += 20
            claim['original_normalized_polygon'] = [[.7, .1], [.9, .1], [.9, .2], [.7, .2]]
            data[4]['pages'][0]['input_blocks'].append({key: deepcopy(claim[key]) for key in (
                *source(1), 'region_array_index', 'block_reading_order', 'block_text', 'original_normalized_polygon')})
    result = score(data)
    assert result['summary']['out_of_scope']['unselected_panel_admissions'] == 1
    assert result['summary']['source_rows']['missing'] == 1
    data[4]['pages'][0]['input_blocks'] = None
    result = score(data)
    assert result['summary']['out_of_scope']['unselected_panel_admissions'] == 0
    assert result['summary']['positive_admissions']['unverified_row_extras'] == 1


def test_multiple_negative_occurrences_are_one_case_group_but_all_false_items_are_counted():
    data = bundle(); data[4]['pages'][1]['items'] = [negative_item(data), negative_item(data)]
    result = score(data)
    assert result['summary']['negative_pages']['false_admitted_items'] == 2
    assert result['summary']['negative_cases']['FALSE_ADMISSION'] == 1
    assert result['details']['negative_cases'][0]['false_admissions'] == 2


def test_a_malformed_abstention_list_or_row_field_does_not_crash_or_claim_field_success():
    data = bundle(); item(data)['abstained_fields'] = None
    result = score(data)
    assert all(field(result, metric)['MISSING'] == 1 for metric in METRICS)
    data = bundle(); item(data)['row_evidence']['item_code'] = None
    item(data)['row_evidence']['value'][0]['region_array_index'] = 999
    result = score(data)
    assert result['summary']['source_rows']['missing'] == 1


def test_execution_error_details_are_retained_privately_and_only_counted_publicly():
    data = bundle(); data[4]['pages'][1].update(status='FAILED', errors=['private parser detail'])
    result = score(data)
    assert result['summary']['execution']['reported_errors'] == 1
    assert 'private parser detail' not in json.dumps(result['summary'])
    assert any(page['errors'] == ['private parser detail'] for page in result['details']['execution'])


def full_synthetic_scope():
    """64 invented files/124 pages; contains none of the private corpus's values."""
    base = bundle(); gold, protocol, evidence, coverage, output = deepcopy(base)
    gold.update(positive_rows=[], negative_pages=[], negative_cases=[])
    evidence['evidence'] = []; coverage['pages'] = []; output['pages'] = []
    protocol['per_field_metrics']['denominator_each'] = 7

    def relocate(value, old, new):
        if isinstance(value, dict):
            return {key: relocate(item, old, new) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item, old, new) for item in value]
        if isinstance(value, str):
            for key in ('source_sha256', 'fixed_ocr_sha256'):
                if value == old[key]:
                    return new[key]
        return value

    for index in range(7):
        origin = source(100 + index)
        row = relocate(base[0]['positive_rows'][0], source(1), origin)
        row.update(gold_id=f'row-{index}', panel_id=f'panel-{index}')
        row['field_evidence_refs'] = {key: f'{index}:{ref}' if ref else None
                                      for key, ref in row['field_evidence_refs'].items()}
        gold['positive_rows'].append(row)
        for old in base[2]['evidence']:
            if old['source_sha256'] == source(1)['source_sha256']:
                proof = relocate(old, source(1), origin)
                proof['evidence_id'] = f'{index}:{old["evidence_id"]}'
                evidence['evidence'].append(proof)
        coverage['pages'].append({**origin, 'status': 'POSITIVE_REVIEWED_PANEL'})
        output['pages'].append(relocate(base[4]['pages'][0], source(1), origin))
    for index, groups in enumerate((5, 5, 4)):
        origin = source(200 + index)
        page = relocate(base[0]['negative_pages'][0], source(2), origin)
        page.update(negative_page_id=f'negative-{index}', case_ids=[f'case-{index}-{i}' for i in range(groups)])
        gold['negative_pages'].append(page)
        proof = relocate(base[2]['evidence'][-1], source(2), origin)
        proof['evidence_id'] = f'negative-proof-{index}'
        evidence['evidence'].append(proof)
        gold['negative_cases'].extend({'case_id': identity, 'negative_page_id': page['negative_page_id'],
                                      'evidence_refs': [proof['evidence_id']]} for identity in page['case_ids'])
        coverage['pages'].append({**origin, 'status': 'NEGATIVE_ADMISSION_BOUNDARY_PAGE'})
        output['pages'].append({'source': origin, 'status': 'COMPLETE', 'items': []})
    for count, files, start, status in [(14, 7, 300, 'PREVIOUSLY_REVIEWED_BUT_OUT_OF_SCOPE'),
                                       (100, 47, 400, 'UNJUDGED_NOT_ORIGINAL_REVIEWED')]:
        for index in range(count):
            origin = {**source(start + index % files), 'manifest_page': 1 + index // files}
            coverage['pages'].append({**origin, 'status': status})
            output['pages'].append({'source': origin, 'status': 'COMPLETE', 'items': [{'disposition': 'ADMITTED'}]})
    return gold, protocol, evidence, coverage, output


def test_full_synthetic_partition_keeps_seven_field_denominators_and_three_negative_pages_separate():
    result = score(full_synthetic_scope())
    assert result['summary']['execution']['fixed_sources'] == 64
    assert result['summary']['execution']['fixed_pages'] == result['summary']['execution']['complete_pages'] == 124
    assert all(field(result, metric)['CORRECT'] == field(result, metric)['denominator'] == 7 for metric in METRICS)
    assert result['summary']['negative_pages']['denominator'] == 3
    assert result['summary']['negative_cases']['denominator'] == 14
    assert result['summary']['out_of_scope']['unreviewed_admissions'] == 100
    assert result['summary']['out_of_scope']['previously_reviewed_admissions'] == 14


def test_identical_values_on_seven_sources_do_not_recover_a_missing_source_by_value_similarity():
    data = full_synthetic_scope()
    wrong_envelope = data[4]['pages'][0]['items'].pop()
    data[4]['pages'][1]['items'].append(wrong_envelope)
    result = score(data)
    assert result['summary']['source_rows'] == {'denominator': 7, 'recovered': 6, 'missing': 1}
    assert field(result, 'value')['CORRECT'] == 6 and field(result, 'value')['MISSING'] == 1
    assert result['summary']['positive_admissions']['unverified_row_extras'] == 1
