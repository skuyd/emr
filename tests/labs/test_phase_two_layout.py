from dataclasses import replace
from pathlib import Path

import pytest

from apps.labs.candidates import extract_lab_candidates
from apps.labs.dictionary import load_dictionary
from apps.labs.extraction import extract_observations
from apps.labs.models import CapabilityLevel, ResultType
from apps.processing.value_objects import OcrPage, OcrRegion


@pytest.fixture
def dictionary():
    return load_dictionary(Path('apps/labs/dictionaries/phase-two.json'))


def page(rows, number=1, metadata=()):
    regions = []
    for y, cells in rows:
        for x, text in cells:
            regions.append(OcrRegion(text, ((x, y), (x + .07, y), (x + .07, y + .025), (x, y + .025)), .99, len(regions)))
    return OcrPage(number, 1000, 1400, tuple(regions), 'fixture', '1', metadata)


def extract(rows, dictionary):
    return extract_observations((page(rows),), dictionary)


def test_explicit_serial_code_and_method_columns_do_not_swallow_project_name(dictionary):
    rows = [(.05, [(.05, '标本：全血')]),
            (.1, [(.03, '序号'), (.12, '项目代号'), (.3, '项目名称'), (.5, '结果'),
                  (.62, '单位'), (.73, '参考范围'), (.86, '检测方法')]),
            (.2, [(.03, '1'), (.12, 'HGB'), (.3, '血红蛋白'), (.5, '130'),
                  (.62, 'g/L'), (.73, '115-150'), (.86, '测试方法甲')])]
    item, = extract(rows, dictionary)
    assert (item.raw_name, item.raw_value, item.raw_unit) == ('血红蛋白', '130', 'g/L')
    assert item.standard_code == 'LAB_HGB'
    assert item.method_raw == '测试方法甲'


def test_agreeing_approved_name_and_abbreviation_fragments_map_without_new_aliases(dictionary):
    item, = extract([(.05, [(.05, '标本：全血')]),
                     (.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, '4 ★PLT 血小板'), (.4, '130'), (.7, '10^9/L')])], dictionary)
    assert item.standard_code == 'LAB_PLT'
    assert item.raw_name == '4 ★PLT 血小板'
    conflict, = extract([(.05, [(.05, '标本：全血')]),
                         (.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                         (.2, [(.05, 'PLT 血红蛋白'), (.4, '130'), (.7, 'g/L')])], dictionary)
    assert conflict.standard_code.startswith('CANDIDATE_')
    assert conflict.capability_level == CapabilityLevel.SEARCH_ONLY


@pytest.mark.parametrize(('raw', 'value', 'flag', 'kind'), [
    ('8.8↑', '8.8', '↑', ResultType.NUMERIC), ('<0.5 L', '<0.5', 'L', ResultType.COMPARATOR),
])
def test_explicit_flag_in_result_cell_preserves_number_comparator_and_source(dictionary, raw, value, flag, kind):
    item, = extract([(.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, 'C反应蛋白'), (.4, raw), (.7, 'mg/L')])], dictionary)
    assert (item.raw_value, item.report_flag_raw, item.result_type) == (value, flag, kind)
    assert raw in item.source_text
    assert item.field_evidence['raw_value'] == item.field_evidence['report_flag_raw']


def test_two_side_by_side_tables_preserve_independent_cells(dictionary):
    rows = [(.1, [( .03, '项目'), (.2, '结果'), (.32, '单位'), (.53, '项目'), (.7, '结果'), (.82, '单位')]),
            (.2, [(.03, 'WBC'), (.2, '5.2'), (.32, '10⁹/L'), (.53, '血红蛋白'), (.7, '130'), (.82, 'g/L')])]
    items = extract(rows, dictionary)
    assert [(x.raw_name, x.raw_value, x.raw_unit) for x in items] == [('WBC', '5.2', '10⁹/L'), ('血红蛋白', '130', 'g/L')]
    assert items[0].field_evidence['raw_value']['polygon'] == [[.2, .2], [.27, .2], [.27, .225], [.2, .225]]
    assert {x.raw_name for x in extract_lab_candidates((page(rows),), 'a' * 64)} == {'WBC', '血红蛋白'}


def test_headers_support_reordered_columns_and_repeated_or_new_tables(dictionary):
    items = extract([(.1, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                     (.2, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '130')]),
                     (.3, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                     (.4, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '131')]),
                     (.5, [(.05, '项目'), (.3, '结果'), (.5, '单位'), (.7, '参考范围')]),
                     (.6, [(.05, '血红蛋白'), (.3, '132'), (.5, 'g/L'), (.7, '115-150')])], dictionary)
    assert [(x.raw_value, x.reference_range_raw) for x in items] == [('130', '115-150'), ('131', '115-150'), ('132', '115-150')]
    assert items[0].reference_range['low'] == '115'


def test_continuation_inherits_headers_but_field_source_is_current_page(dictionary):
    pages = (page([(.1, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                   (.2, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '130')])]),
             page([(.1, [(.05, '续表')]), (.2, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '131')])], 2))
    items = extract_observations(pages, dictionary)
    assert [x.raw_value for x in items] == ['130', '131']
    assert items[1].field_evidence['raw_value']['page_number'] == 2


def test_missing_left_result_never_borrows_right_result_or_reference(dictionary):
    items = extract([(.1, [(.03, '项目'), (.2, '结果'), (.32, '单位'), (.53, '项目'), (.7, '结果'), (.82, '单位')]),
                     (.2, [(.03, 'WBC'), (.32, '10^9/L'), (.53, '血红蛋白'), (.7, '130'), (.82, 'g/L')])], dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [('WBC', ''), ('血红蛋白', '130')]
    assert items[0].capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in items[0].quality_issues}


def test_multiple_values_in_one_cell_preserve_candidates_without_trusted_number(dictionary):
    items = extract([(.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, '血红蛋白'), (.4, '130'), (.43, '140'), (.7, 'g/L')])], dictionary)
    assert len(items) == 1
    assert items[0].raw_value == '130 140'
    assert items[0].capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in items[0].quality_issues}


def test_headerless_two_columns_use_name_anchors(dictionary):
    items = extract([(.2, [(.03, '血红蛋白'), (.2, '130'), (.32, 'g/L'), (.53, 'C反应蛋白'), (.7, '<0.5'), (.82, 'mg/L')])], dictionary)
    assert [(x.raw_value, x.raw_unit) for x in items] == [('130', 'g/L'), ('<0.5', 'mg/L')]


@pytest.mark.parametrize(('value', 'want'), [('12.3', ResultType.NUMERIC), ('≤0.5', ResultType.COMPARATOR), ('阴性', ResultType.QUALITATIVE), ('++', ResultType.SEMI_QUANTITATIVE), ('溶血', ResultType.STATUS)])
def test_five_result_kinds_preserve_original_value(dictionary, value, want):
    item, = extract([(.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, 'C反应蛋白'), (.4, value), (.7, 'mg/L')])], dictionary)
    assert (item.raw_value, item.result_type) == (value, want)


@pytest.mark.parametrize(('raw', 'candidate'), [('1,23', '1.23'), ('＜ 0．5', '< 0.5'), ('O.5', '0.5')])
def test_repairs_are_candidates_and_do_not_replace_raw_values(dictionary, raw, candidate):
    item, = extract([(.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, 'C反应蛋白'), (.4, raw), (.7, 'mg/L')])], dictionary)
    assert item.raw_value == raw
    assert any(x['before'] == raw and x['after'] == candidate for x in item.normalization_candidates)
    assert 'normalization_uncertain' in {x['code'] for x in item.quality_issues}


def test_explicit_context_disambiguates_wbc_and_pct_without_guessing_specimen(dictionary):
    items = extract([(.05, [(.05, '标本：全血'), (.4, '血常规')]),
                     (.1, [(.05, '项目'), (.4, '结果'), (.7, '单位')]),
                     (.2, [(.05, 'WBC'), (.4, '5.2'), (.7, '10^9/L')]),
                     (.3, [(.05, 'PCT'), (.4, '0.2'), (.7, '%')])], dictionary)
    assert [(x.standard_code, x.specimen) for x in items] == [('LAB_WBC', 'BLOOD'), ('LAB_PLATELETCRIT', 'BLOOD')]
    assert items[0].field_evidence['specimen']['precision'] == 'region'
    unknown, = extract([(.2, [(.05, 'PCT'), (.4, '0.2'), (.7, '%')])], dictionary)
    assert unknown.standard_code.startswith('CANDIDATE_')
    assert unknown.specimen == ''


def test_low_confidence_rows_remain_reviewable(dictionary):
    source = page([(.2, [(.05, '血红蛋白'), (.4, '130'), (.7, 'g/L')])])
    source = replace(source, regions=tuple(replace(x, confidence=.2) for x in source.regions))
    item, = extract_observations((source,), dictionary)
    assert item.raw_value == '130'
    assert item.capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'recognition_uncertain' in {x['code'] for x in item.quality_issues}


def test_approximate_page_regions_do_not_claim_precise_field_locations(dictionary):
    source = page([(.2, [(.05, '血红蛋白'), (.4, '130'), (.7, 'g/L')])], metadata={'region_precision': 'page'})
    item, = extract_observations((source,), dictionary)
    assert item.field_evidence['raw_value'] == {'page_number': 1, 'polygon': None, 'precision': 'page'}


def test_headerless_missing_value_preserves_left_item_without_swallowing_right(dictionary):
    items = extract([(.2, [(.03, '血红蛋白'), (.32, 'g/L'), (.53, 'C反应蛋白'), (.7, '<0.5'), (.82, 'mg/L')])], dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [('血红蛋白', ''), ('C反应蛋白', '<0.5')]
    assert items[0].field_evidence['raw_value']['precision'] == 'page'


def test_numeric_reference_is_not_used_as_a_missing_result(dictionary):
    item, = extract([(.1, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                     (.2, [(.05, '血红蛋白'), (.3, '150'), (.5, 'g/L')])], dictionary)
    assert (item.raw_value, item.reference_range_raw) == ('', '150')
    assert item.result_type == ResultType.STATUS


def test_ambiguous_column_boundary_is_never_a_trusted_value(dictionary):
    item, = extract([(.1, [(.05, '项目'), (.3, '结果'), (.5, '参考范围'), (.7, '单位')]),
                     (.2, [(.05, '血红蛋白'), (.4, '130'), (.7, 'g/L')])], dictionary)
    assert item.capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in item.quality_issues}
    assert '130' in item.source_text


def test_unreliable_specimen_context_cannot_resolve_an_ambiguous_alias(dictionary):
    source = page([(.05, [(.05, '标本：全血')]), (.2, [(.05, '白细胞'), (.4, '5.2'), (.7, '10^9/L')])])
    source = replace(source, regions=(replace(source.regions[0], confidence=.2), *source.regions[1:]))
    item, = extract_observations((source,), dictionary)
    assert item.standard_code.startswith('CANDIDATE_')
    assert 'recognition_uncertain' in {x['code'] for x in item.quality_issues}


def test_headerless_unlabeled_multiple_numbers_require_association_review(dictionary):
    item, = extract([(.2, [(.05, '血红蛋白'), (.3, '130'), (.5, '140'), (.7, 'g/L')])], dictionary)
    assert item.capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in item.quality_issues}


def test_separate_reports_do_not_inherit_previous_page_column_order(dictionary):
    pages = (page([(.1, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                   (.2, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '130')])]),
             page([(.2, [(.05, '血红蛋白'), (.3, '131'), (.5, 'g/L'), (.7, '115-150')])], 2))
    assert [x.raw_value for x in extract_observations(pages, dictionary)] == ['130', '131']


def test_single_ocr_span_with_page_polygon_preserves_results_with_page_evidence(dictionary):
    source = OcrPage(1, 1000, 1400, (OcrRegion('血红蛋白 130 g/L', ((0, 0), (1, 0), (1, 1), (0, 1)), .99, 0),), 'fixture', '1')
    item, = extract_observations((source,), dictionary)
    assert (item.raw_name, item.raw_value, item.raw_unit) == ('血红蛋白', '130', 'g/L')
    assert item.field_evidence['raw_value']['precision'] == 'page'


def test_superscript_unit_candidate_preserves_exponent_instead_of_changing_magnitude(dictionary):
    item, = extract([(.2, [(.05, 'WBC'), (.4, '5.2'), (.7, '10⁹/L')])], dictionary)
    assert item.raw_unit == '10⁹/L'
    assert any(x['after'] == '10^9/L' for x in item.normalization_candidates)
    assert not any(x['after'] == '109/L' for x in item.normalization_candidates)


def test_low_confidence_headers_cannot_publish_trusted_associations(dictionary):
    source = page([(.1, [(.05, '项目'), (.3, '参考范围'), (.5, '单位'), (.7, '结果')]),
                   (.2, [(.05, '血红蛋白'), (.3, '115-150'), (.5, 'g/L'), (.7, '130')])])
    source = replace(source, regions=tuple(replace(x, confidence=.2) if i < 4 else x for i, x in enumerate(source.regions)))
    item, = extract_observations((source,), dictionary)
    assert item.capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in item.quality_issues}


def test_headerless_suspicious_decimal_retains_a_repair_candidate(dictionary):
    item, = extract([(.2, [(.05, 'C反应蛋白'), (.4, 'O.5'), (.7, 'mg/L')])], dictionary)
    assert item.raw_value == 'O.5'
    assert any(x['after'] == '0.5' for x in item.normalization_candidates)


def test_detached_comparator_is_associated_with_number_without_entering_name(dictionary):
    item, = extract([(.2, [(.05, 'C反应蛋白'), (.35, '<'), (.4, '0.5'), (.7, 'mg/L')])], dictionary)
    assert (item.raw_name, item.raw_value, item.result_type) == ('C反应蛋白', '< 0.5', ResultType.COMPARATOR)


def test_candidate_api_retains_semi_quantitative_labels():
    candidates = extract_lab_candidates((page([(.2, [(.05, '尿蛋白'), (.4, '++')])]),), 'a' * 64)
    assert [x.raw_name for x in candidates] == ['尿蛋白']


def test_partial_headers_infer_explicit_unit_and_do_not_turn_footer_into_result(dictionary):
    items = extract([(.1, [(.05, '项目'), (.4, '结果')]),
                     (.2, [(.05, '血红蛋白'), (.4, '130'), (.7, 'g/L')]),
                     (.3, [(.05, '备注信息')])], dictionary)
    assert [(x.raw_name, x.raw_value, x.raw_unit) for x in items] == [('血红蛋白', '130', 'g/L')]


@pytest.mark.parametrize('left_x,right_x', [(.03, .53), (.39, .47)])
def test_side_by_side_specimens_have_independent_context_and_source(dictionary, left_x, right_x):
    items = extract([(.05, [(left_x, '标本：尿液'), (right_x, '标本：全血')]),
                     (.1, [(.03, 'item'), (.2, 'result'), (.32, 'unit'), (.53, 'item'), (.7, 'result'), (.82, 'unit')]),
                     (.2, [(.03, 'GLU'), (.2, '5.2'), (.32, 'mmol/L'), (.53, 'GLU'), (.7, '6.2'), (.82, 'mmol/L')])], dictionary)
    assert [item.specimen for item in items] == ['URINE', 'BLOOD']
    assert items[0].standard_code != items[1].standard_code
    assert items[0].field_evidence['specimen']['polygon'][0][0] == left_x


def test_region_spanning_two_columns_cannot_be_a_trusted_result(dictionary):
    source = page([(.1, [(.05, 'item'), (.3, 'result'), (.5, 'reference'), (.8, 'unit')]),
                   (.2, [(.05, 'HGB'), (.3, '130'), (.8, 'g/L')])])
    regions = list(source.regions)
    regions[5] = replace(regions[5], polygon=((.3, .2), (.65, .2), (.65, .225), (.3, .225)))
    item, = extract_observations((replace(source, regions=tuple(regions)),), dictionary)
    assert item.raw_value == '130'
    assert item.capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in item.quality_issues}


@pytest.mark.parametrize('with_headers', [True, False])
def test_region_spanning_two_item_rows_cannot_be_a_trusted_result(dictionary, with_headers):
    rows = [(.1, [(.05, 'item'), (.4, 'result'), (.7, 'unit')])] if with_headers else []
    rows.extend([(.2, [(.05, 'HGB'), (.7, 'g/L')]), (.24, [(.05, 'MCHC'), (.7, 'g/L')])])
    source = page(rows)
    value = OcrRegion('330', ((.4, .21), (.47, .21), (.47, .265), (.4, .265)), .99, 99)
    items = extract_observations((replace(source, regions=(*source.regions, value)),), dictionary)
    assert items
    assert all(item.capability_level == CapabilityLevel.SEARCH_ONLY for item in items)
    assert all('association_conflict' in {x['code'] for x in item.quality_issues} for item in items)


def test_method_header_is_preserved_without_polluting_report_reference(dictionary):
    item, = extract([(.1, [(.03, '项目名称'), (.3, '结果'), (.45, '单位'), (.6, '参考范围'), (.8, '检测方法')]),
                     (.2, [(.03, '血红蛋白'), (.3, '130'), (.45, 'g/L'), (.6, '115-150'), (.8, '合成方法')])], dictionary)
    assert item.method_raw == '合成方法'
    assert item.reference_range_raw == '115-150'
    assert item.field_evidence['method_raw']['polygon'][0][0] == .8
