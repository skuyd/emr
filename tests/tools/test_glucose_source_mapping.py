"""Invented OCR and real synthetic persistence; never opens a private gold/cache."""
from copy import deepcopy
import hashlib

import pytest

from tools.glucose_source_mapping import FixedOcrMapper, MappingInputError, map_candidate, map_pipeline_document


SOURCE_SHA = hashlib.sha256(b'synthetic-source').hexdigest()
OCR_SHA = hashlib.sha256(b'synthetic-fixed-ocr').hexdigest()


def box(left, top, right, bottom):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def synthetic(*, raw_value='5.50', unit='mmo1/L', fasting=True, padding=False):
    from apps.glucose.payloads import normalize_payload
    from apps.glucose.source_context import report_context

    texts = ['合成检验报告单', '检验项目：空腹血糖' if fasting else '检验项目：生化',
             '标本：血清', '采样时间：2024-04-01 07:08:09', '报告时间：2024-04-01 10:11:12',
             'GLU', '葡萄糖', raw_value, unit]
    rectangles = [box(.05, .02, .95, .05), box(.05, .08, .7, .11), box(.05, .14, .35, .17),
                  box(.05, .20, .8, .23), box(.05, .26, .8, .29),
                  box(.05, .40, .15, .43), box(.20, .40, .35, .43),
                  box(.45, .40, .55, .43), box(.65, .40, .85, .43)]
    raw = [{'text': (' \t' + text + '  ') if padding else text, 'polygon': polygon,
            'reading_order': order, 'confidence': .99}
           for order, (text, polygon) in enumerate(zip(texts, rectangles))]
    # Original array order deliberately differs from the app's reading order.
    pages = [{'page_number': 1, 'width': 1000, 'height': 1000, 'regions': list(reversed(raw))}]
    blocks = [{'id': f'b{order}', 'text': text, 'polygon': polygon, 'reading_order': order,
               'document_page__page_number': 1, 'document_page_id': 'page-1',
               'parsing_version_id': 'parse-1', 'parsing_version__document_id': 'document-1'}
              for order, (text, polygon) in enumerate(zip(texts, rectangles))]
    context = report_context(blocks, anchor_polygon=rectangles[7])
    sample = context['sample_time']
    data = normalize_payload({'value': raw_value, 'unit': unit, 'measured_local': sample['local'],
        'time_precision': sample['precision'], 'timezone': sample['timezone'],
        'timezone_origin': sample['timezone_origin'], 'time_slot': context['time_slot'],
        'source_label': context['specimen_raw'], 'notes': ''}, source_kind='LAB_REPORT', allow_imprecise=True)
    fields = {}
    for field, index, value in [('raw_name', 6, texts[6]), ('raw_value', 7, raw_value),
                               ('raw_unit', 8, unit), ('specimen', 2, 'BLOOD'),
                               ('standard_code', 5, 'LAB_FASTING_GLUCOSE')]:
        polygon = rectangles[index] if field != 'standard_code' else box(.05, .40, .35, .43)
        fields[field] = {'document_id': 'document-1', 'parsing_version_id': 'parse-1',
            'page_id': 'page-1', 'page_number': 1, 'observation_id': 'observation-1',
            'revision_id': None, 'effective_value': value, 'original_text': 'GLU 葡萄糖 ' + raw_value + ' ' + unit,
            'original_polygon': box(.05, .40, .85, .43),
            'field_evidence': {'page_number': 1, 'polygon': polygon, 'precision': 'region'}}
    data['source'] = {'document_id': 'document-1', 'source_sha256': SOURCE_SHA, 'page_number': 1,
        'page_id': 'page-1', 'parsing_version_id': 'parse-1', 'observation_id': 'observation-1',
        'field_sources': fields, 'report_context': context}
    data['field_origins'] = {'time': 'SOURCE_OCR', 'time_slot': 'SOURCE_OCR' if fasting else 'NOT_STATED'}
    return pages, blocks, {'data': data, 'source_fingerprint': 'synthetic-fingerprint'}


def mapper(pages, blocks):
    return FixedOcrMapper(pages, blocks, source_sha256=SOURCE_SHA, fixed_ocr_sha256=OCR_SHA,
                         document_id='document-1')


def proof_text(proofs):
    return ''.join(p['block_text'][p['char_start']:p['char_end_exclusive']] for p in proofs)


def test_unknown_raw_unit_does_not_erase_numeric_value_or_repair_digit_one():
    pages, blocks, candidate = synthetic()
    assert candidate['data']['normalized_value'] is None
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['decimal_value'] == '5.50'
    assert item['values']['raw_value'] == '5.50'
    assert item['values']['unit'] == item['values']['raw_unit'] == 'mmo1/L'
    assert proof_text(item['field_evidence']['value']) == '5.50'
    assert proof_text(item['field_evidence']['unit']) == 'mmo1/L'
    assert proof_text(item['field_evidence']['item_code']) == 'GLU'
    assert proof_text(item['field_evidence']['item_name']) == '葡萄糖'


def test_original_array_index_reading_order_padding_offsets_and_seconds_are_preserved():
    pages, blocks, candidate = synthetic(padding=True)
    result = map_candidate(candidate, mapper(pages, blocks))
    value = result['field_evidence']['value'][0]
    assert (value['region_array_index'], value['block_reading_order']) == (1, 7)
    assert (value['char_start'], value['char_end_exclusive']) == (2, 6)
    assert value['block_text'] == ' \t5.50  '
    assert value['full_block_text_sha256'] == hashlib.sha256(value['block_text'].encode()).hexdigest()
    assert result['mapping_receipt']['text_adjustments']
    for role, hour, label in [('sampling', '07:08:09', '采样时间'), ('reporting', '10:11:12', '报告时间')]:
        current = result['values'][role]
        assert current['local_datetime'] == '2024-04-01T' + hour
        assert current['raw'] == '2024-04-01 ' + hour
        assert current['precision'] == 'SECOND'
        assert current['timezone_status'] == 'UNCONFIRMED'
        assert all(current[key] is None for key in ('printed_timezone', 'printed_utc_offset', 'timezone_name', 'utc_offset', 'utc_datetime'))
        assert proof_text(result['field_evidence'][role + '_label']) == label
        assert proof_text(result['field_evidence'][role + '_time']) == '2024-04-01 ' + hour


@pytest.mark.parametrize('target', ['sha', 'page', 'text', 'polygon', 'start', 'reading_order'])
def test_contradictory_context_locators_are_not_discarded_to_recover_a_matching_time(target):
    pages, blocks, candidate = synthetic()
    span = candidate['data']['source']['report_context']['sample_time']['evidence'][0]
    if target == 'sha':
        span['source_sha256'] = '0' * 64
    elif target == 'page':
        span['manifest_page'] = 2
    elif target == 'text':
        span['text'] = span['text'].replace('07:', '08:')
    elif target == 'polygon':
        span['polygon'] = blocks[4]['polygon']
    elif target == 'start':
        span['start'] = 10000
    else:
        span['reading_order'] = 99
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['field_evidence']['sampling_time'] == []
    assert item['field_evidence']['sampling_label'] == []
    assert item['mapping_receipt']['issues']


def test_missing_or_wrong_field_location_does_not_search_same_page_for_expected_value():
    pages, blocks, candidate = synthetic()
    field = candidate['data']['source']['field_sources']['raw_value']['field_evidence']
    field['polygon'] = blocks[8]['polygon']
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['field_evidence']['value'] == []
    field.clear()
    assert map_candidate(candidate, mapper(pages, blocks))['row_evidence']['value'] == []


def test_changed_value_keeps_original_row_location_independent_of_predicted_text():
    pages, blocks, candidate = synthetic()
    candidate['data']['raw_value'] = '9.9'
    candidate['data']['source']['field_sources']['raw_value']['effective_value'] = '9.9'
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['values']['decimal_value'] == '9.9'
    assert result['field_evidence']['value'] == []
    assert proof_text(result['row_evidence']['value']) == '5.50'


@pytest.mark.parametrize('key,value', [('page_id', 'different-page'), ('parsing_version_id', 'different-parse'),
    ('document_id', 'different-document'), ('block_text', 'altered'),
    ('full_block_text_sha256', '0' * 64), ('char_start', 999), ('char_end_exclusive', 999)])
def test_additional_supplied_context_identity_and_offset_must_also_agree(key, value):
    pages, blocks, candidate = synthetic()
    candidate['data']['source']['report_context']['sample_time']['evidence'][0][key] = value
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['field_evidence']['sampling_time'] == []


def test_duplicate_original_reading_order_is_invalid_even_with_one_matching_text():
    pages, blocks, _candidate = synthetic()
    duplicate = deepcopy(pages[0]['regions'][0])
    duplicate['text'] = 'other content'
    pages[0]['regions'].append(duplicate)
    with pytest.raises(MappingInputError):
        mapper(pages, blocks)


def test_duplicate_literal_and_normalized_character_do_not_invent_raw_offsets():
    pages, blocks, candidate = synthetic(raw_value='５.５０')
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['values']['decimal_value'] == '5.50'
    assert proof_text(result['field_evidence']['value']) == '５.５０'
    blocks[7]['text'] = '5.50'
    assert map_candidate(candidate, mapper(pages, blocks))['field_evidence']['value'] == []
    pages, blocks, candidate = synthetic()
    blocks[7]['text'] = pages[0]['regions'][1]['text'] = '5.50 5.50'
    assert map_candidate(candidate, mapper(pages, blocks))['field_evidence']['value'] == []


def test_field_page_id_and_original_polygon_contradictions_fail_closed():
    pages, blocks, candidate = synthetic()
    field = candidate['data']['source']['field_sources']['raw_value']
    field['page_id'] = 'other-page'
    assert map_candidate(candidate, mapper(pages, blocks))['field_evidence']['value'] == []
    field['page_id'] = 'page-1'
    field['field_evidence']['page_number'] = 2
    assert map_candidate(candidate, mapper(pages, blocks))['field_evidence']['value'] == []


def test_geometry_uses_original_transform_and_never_layout_polygon_as_original():
    from apps.processing.geometry import source_polygon
    pages, blocks, candidate = synthetic()
    matrix = [[.8, 0, .1], [0, .8, .1], [0, 0, 1]]
    pages[0]['source_transform'] = matrix
    for block in blocks:
        block['polygon'] = [list(p) for p in source_polygon(matrix, block['polygon'])]
    for source in candidate['data']['source']['field_sources'].values():
        source['field_evidence']['polygon'] = [list(p) for p in source_polygon(matrix, source['field_evidence']['polygon'])]
    value = map_candidate(candidate, mapper(pages, blocks))['field_evidence']['value'][0]
    assert value['original_normalized_polygon'] == blocks[7]['polygon']
    assert value['original_normalized_polygon'] != pages[0]['regions'][1]['polygon']


def test_no_printed_code_is_not_fabricated_from_internal_dictionary_identity():
    pages, blocks, candidate = synthetic()
    candidate['data']['source']['field_sources'].pop('standard_code')
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['source_type'] == 'LABORATORY_REPORT'
    assert item['field_evidence']['item_code'] == []
    assert item['row_evidence']['item_name']


def test_no_heading_means_not_stated_and_user_fasting_is_not_a_printed_heading():
    pages, blocks, candidate = synthetic(fasting=False)
    assert map_candidate(candidate, mapper(pages, blocks))['values']['meal_context'] == {
        'status': 'NOT_STATED', 'basis': 'NO_EXPLICIT_MEAL_CONDITION_ON_ORIGINAL_PANEL',
        'actual_preparation_independently_verified': False, 'clock_inference_allowed': False}
    candidate['data']['time_slot'] = 'FASTING'
    candidate['data']['field_origins']['time_slot'] = 'LAB_REVISION'
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['values']['meal_context']['basis'] == 'LAB_REVISION'
    assert result['field_evidence']['meal_context'] == []


def test_verified_date_change_preserves_actual_day_and_original_sample_seconds_separately():
    pages, blocks, candidate = synthetic()
    candidate['data'].update(local_time='2024-04-02', time_precision='DAY', measured_at=None)
    candidate['data']['field_origins']['time'] = 'LAB_REVISION'
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['values']['sampling']['local_datetime'] == '2024-04-02'
    assert result['values']['sampling']['precision'] == 'DAY'
    assert result['values']['sampling']['raw'] == '2024-04-01 07:08:09'


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '<5.5', '5.5-8.0'])
def test_decimal_output_does_not_treat_non_single_numeric_results_as_measurements(raw):
    pages, blocks, candidate = synthetic()
    candidate['data'].update(raw_value=raw, result_type='NUMERIC')
    assert map_candidate(candidate, mapper(pages, blocks))['values']['decimal_value'] is None


def test_input_document_hash_and_duplicate_manifest_page_are_rejected():
    pages, blocks, candidate = synthetic()
    candidate['data']['source']['source_sha256'] = '0' * 64
    with pytest.raises(MappingInputError):
        map_candidate(candidate, mapper(pages, blocks))
    with pytest.raises(MappingInputError):
        mapper(pages + deepcopy(pages), blocks)


def test_cross_block_label_and_timestamp_keep_each_original_fragment_in_order():
    pages, blocks, candidate = synthetic()
    first, second = blocks[3], deepcopy(blocks[3])
    pages[0]['regions'][5]['text'] = '采样时间：2024-04-01 '
    first['text'] = pages[0]['regions'][5]['text'].strip()
    first['polygon'] = pages[0]['regions'][5]['polygon'] = box(.05, .20, .5, .23)
    second.update(id='clock-tail', text='07:08:09', reading_order=30, polygon=box(.51, .20, .8, .23))
    blocks.append(second)
    pages[0]['regions'].append({'text': second['text'], 'reading_order': 30, 'polygon': second['polygon']})
    candidate['data']['source']['report_context']['sample_time']['evidence'] = [
        {'block_id': b['id'], 'text': b['text'], 'start': 0, 'end': len(b['text']), 'polygon': b['polygon']}
        for b in (first, second)]
    item = map_candidate(candidate, mapper(pages, blocks))
    proofs = item['field_evidence']['sampling_time']
    assert [p['region_array_index'] for p in proofs] == [5, 9]
    assert [p['block_reading_order'] for p in proofs] == [3, 30]
    assert [p['block_text'][p['char_start']:p['char_end_exclusive']] for p in proofs] == ['2024-04-01', '07:08:09']
    assert proof_text(item['field_evidence']['sampling_label']) == '采样时间'


def test_cross_page_candidate_proof_is_not_relabelled_to_the_item_envelope():
    pages, blocks, candidate = synthetic()
    candidate['data']['source']['page_number'] = 2
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['source']['manifest_page'] == 2
    assert item['row_evidence']['value'][0]['manifest_page'] == 1


def test_unknown_sampling_context_stays_unknown_even_with_a_known_report_time():
    pages, blocks, candidate = synthetic()
    candidate['data'].update(local_time='', time_precision='UNKNOWN', measured_at=None)
    candidate['data']['source']['report_context']['sample_time'] = {
        'local': '', 'raw': '', 'raw_label': '', 'precision': 'UNKNOWN', 'role': None,
        'evidence': [], 'timezone_origin': 'UNCONFIRMED', 'timezone': '', 'utc_datetime': None}
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['sampling']['local_datetime'] is None
    assert item['values']['sampling']['role'] is None
    assert item['field_evidence']['sampling_time'] == []
    assert item['values']['reporting']['local_datetime'] == '2024-04-01T10:11:12'


@pytest.mark.parametrize('key', ['char_start', 'char_end_exclusive', 'start', 'end'])
def test_field_polygon_does_not_override_a_contradictory_supplied_character_locator(key):
    pages, blocks, candidate = synthetic()
    candidate['data']['source']['field_sources']['raw_value']['field_evidence'][key] = 900
    result = map_candidate(candidate, mapper(pages, blocks))
    assert result['field_evidence']['value'] == []


def test_no_printed_timezone_is_not_confused_with_explicit_utc_or_user_confirmation():
    pages, blocks, candidate = synthetic()
    clock = candidate['data']['source']['report_context']['sample_time']
    clock.update(timezone='UTC', timezone_origin='SOURCE_EXPLICIT', utc_datetime='2024-04-01T07:08:09+00:00')
    candidate['data'].update(timezone='UTC', timezone_origin='SOURCE_EXPLICIT', utc_offset='+00:00',
                             measured_at='2024-04-01T07:08:09+00:00')
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['sampling']['printed_timezone'] == 'UTC'
    assert item['values']['sampling']['utc_datetime'] == '2024-04-01T07:08:09+00:00'
    # A user-confirmed zone changes execution values, not original printed facts.
    clock.update(timezone='', timezone_origin='UNCONFIRMED', utc_datetime=None)
    candidate['data'].update(timezone='Asia/Shanghai', timezone_origin='USER_CONFIRMED', utc_offset='+08:00',
                             measured_at='2024-03-31T23:08:09+00:00')
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['sampling']['printed_timezone'] is None
    assert item['values']['sampling']['timezone_status'] == 'USER_CONFIRMED'


@pytest.mark.django_db(transaction=True)
def test_absent_current_version_is_not_a_successful_empty_negative_page(django_user_model):
    from tests.glucose.factories import lab_source
    _client, _patient, document, version, _observation = lab_source(django_user_model)
    pages = fixed_from_version(version)
    version.active = False
    version.save(update_fields=['active'])
    result = map_pipeline_document(document, pages, source_sha256=document.sha256, fixed_ocr_sha256=OCR_SHA,
                                   execution_status={1: 'COMPLETE'})
    assert result['pages'][0]['status'] == 'FAILED'
    assert result['pages'][0]['pipeline_status'] == 'COMPLETE'
    assert result['pages'][0]['errors'][0]['reason'] == 'no_current_published_parsing_version'


def test_mg_dl_value_is_not_replaced_with_the_apps_converted_mmol_value():
    pages, blocks, candidate = synthetic(raw_value='100', unit='mg/dL')
    assert candidate['data']['normalized_value'] == '5.551'
    item = map_candidate(candidate, mapper(pages, blocks))
    assert item['values']['decimal_value'] == '100'
    assert item['values']['unit'] == item['values']['raw_unit'] == 'mg/dL'


def scoring_bundle_for_synthetic(pages):
    """Independent literal assertions for the invented page, not mapper output."""
    from tests.tools.test_glucose_source_evaluation import bundle
    data = bundle()
    source = {'source_sha256': SOURCE_SHA, 'fixed_ocr_sha256': OCR_SHA, 'manifest_page': 1}
    data[0]['positive_rows'][0]['source'] = deepcopy(source)
    data[3]['pages'][0].update(source)
    data[4]['pages'][0]['source'] = deepcopy(source)
    own = []
    for role, order, literal in [('item_code', 5, 'GLU'), ('item_name', 6, '葡萄糖'), ('value', 7, '5.50'),
        ('unit', 8, 'mmol/L'), ('specimen', 2, '血清'), ('sampling_label', 3, '采样时间'),
        ('sampling_time', 3, '2024-04-01 07:08:09'), ('reporting_label', 4, '报告时间'),
        ('reporting_time', 4, '2024-04-01 10:11:12'), ('meal_context', 1, '检验项目：空腹血糖')]:
        index, block = next((index, b) for index, b in enumerate(pages[0]['regions']) if b['reading_order'] == order)
        start = block['text'].index(literal)
        own.append({'evidence_id': role, **source, 'fixed_ocr_fragments': [{
            'region_array_index': index, 'block_reading_order': order, 'char_start': start,
            'char_end_exclusive': start + len(literal), 'raw_fixed_ocr_fragment': literal,
            'full_block_text_sha256': hashlib.sha256(block['text'].encode()).hexdigest(),
            'original_normalized_polygon': block['polygon']}]})
    data[2]['evidence'] = own + [e for e in data[2]['evidence'] if e['evidence_id'] == 'excluded-range']
    return data


@pytest.mark.parametrize('changed_value', [False, True])
def test_approved_neutral_scorer_keeps_row_identity_separate_from_value_correctness(changed_value):
    from tools.glucose_source_evaluation import score_predictions
    pages, blocks, candidate = synthetic(unit='mmol/L')
    if changed_value:
        candidate['data']['raw_value'] = '9.9'
    index = mapper(pages, blocks)
    item = map_candidate(candidate, index)
    data = scoring_bundle_for_synthetic(pages)
    data[4]['pages'][0].update(items=[item], input_blocks=index.pages[1])
    result = score_predictions(*data)['summary']
    assert result['source_rows'] == {'denominator': 1, 'recovered': 1, 'missing': 0}
    for key, counts in result['fields'].items():
        expected = 'MISMATCH' if changed_value and key in ('value', 'raw_value_retained') else 'CORRECT'
        assert counts[expected] == 1, (key, counts)


def fixed_from_version(version):
    grouped = {}
    for block in version.ocr_blocks.select_related('document_page').order_by('reading_order'):
        grouped.setdefault(block.document_page.page_number, []).append({
            'text': block.text, 'polygon': block.polygon, 'reading_order': block.reading_order, 'confidence': .99})
    return [{'page_number': number, 'regions': regions} for number, regions in grouped.items()]


@pytest.mark.django_db(transaction=True)
def test_real_candidates_preserve_every_rejection_adapter_failure_and_failed_page(django_user_model):
    from apps.labs.revisions import revise_observation
    from tests.glucose.factories import lab_source
    _client, patient, document, version, observation = lab_source(django_user_model, value='5.50', unit='mmo1/L')
    pages = fixed_from_version(version) + [{'page_number': 2, 'regions': []}, {'page_number': 3, 'regions': []}]
    kwargs = {'source_sha256': document.sha256, 'fixed_ocr_sha256': OCR_SHA,
              'execution_status': {1: 'FAILED', 2: 'NOT_RUN', 3: 'COMPLETE'}}
    initial = map_pipeline_document(document, pages, **kwargs)
    assert [p['status'] for p in initial['pages']] == ['FAILED', 'NOT_RUN', 'COMPLETE']
    assert initial['pages'][0]['items'][0]['disposition'] == 'ADMITTED'
    assert initial['pages'][0]['items'][0]['values']['decimal_value'] == '5.50'
    revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_value': '1' * 161}, expected_revision=0)
    kwargs['execution_status'][1] = 'COMPLETE'
    failure = map_pipeline_document(document, pages, **kwargs)
    page = failure['pages'][0]
    assert page['status'] == 'FAILED' and page['pipeline_status'] == 'COMPLETE'
    assert page['items'][0]['disposition'] == 'ABSTAINED'
    assert page['items'][0]['mapping_receipt']['diagnostic']['cause_type'] == 'GlucoseInputError'
    assert page['items'][0]['mapping_receipt']['diagnostic']['effective_raw_value'] == '1' * 161
    revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_name': '糖化血红蛋白'}, expected_revision=1)
    excluded = map_pipeline_document(document, pages, **kwargs)['pages'][0]
    assert excluded['status'] == 'COMPLETE'
    assert excluded['items'][0]['disposition'] == 'EXCLUDED'
    assert excluded['items'][0]['diagnostic_evidence']


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('separate_code', [False, True])
def test_real_synthetic_ocr_pipeline_is_mapped_from_persisted_candidate_without_mocking_it(django_user_model, separate_code):
    from apps.processing.models import ParsingVersion
    from apps.labs.dictionary import phase_two_dictionary
    from apps.processing.ocr.fake import FixtureOcrProvider
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from apps.processing.value_objects import OcrPage, OcrRegion
    from tests.processing.test_pipeline import _document_and_run, _png_bytes, _Store
    pages, _blocks, _candidate = synthetic(unit='mmol/L')
    raw = pages[0]
    if not separate_code:
        raw['regions'] = [r for r in raw['regions'] if r['text'] != 'GLU']
    page = OcrPage(1, 100, 100, tuple(OcrRegion(r['text'], r['polygon'], .99, r['reading_order'])
                   for r in raw['regions']), 'fixture', '1.0')
    document, run = _document_and_run(django_user_model)
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)),
                                          dictionary=phase_two_dictionary())
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.lab_observations.count() == 1
    result = map_pipeline_document(document, pages, source_sha256=document.sha256,
                                   fixed_ocr_sha256=OCR_SHA, execution_status={1: 'COMPLETE'})
    item = result['pages'][0]['items'][0]
    if separate_code:
        # This is the current real adapter's rejection, not a mapper repair.
        assert item['disposition'] == 'EXCLUDED'
        assert item['mapping_receipt']['diagnostic']['effective_raw_name'] == 'GLU 葡萄糖'
        assert item['diagnostic_evidence']
        return
    observed = version.lab_observations.get()
    assert item['disposition'] == 'ADMITTED', (item.get('mapping_receipt'), observed.standard_code, observed.specimen)
    assert item['values']['decimal_value'] == '5.50'
    assert proof_text(item['field_evidence']['value']) == '5.50'
    assert proof_text(item['field_evidence']['sampling_label']) == '采样时间'
    assert proof_text(item['field_evidence']['reporting_label']) == '报告时间'
    assert item['values']['sampling']['role'] == 'SPECIMEN_SAMPLING'
    assert item['values']['reporting']['role'] == 'LAB_REPORT_ISSUANCE'
    assert result['mapping_receipt']['parsing_version_id'] == str(version.pk)
