from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from apps.labs import catalog
from apps.labs.extraction import _unit_key


def test_catalog_covers_the_verified_source():
    data = catalog.load_catalog()
    assert data.source_sha256 == '699f659730683792de0f98dc27a05c76fe75980f3b76adf0024497812154b006'
    assert len(data.source_rows) == 201
    assert len({item.code for item in data.indicators}) == len(data.indicators) == 201
    assert data.groups == ('血常规', '性激素6项', '肿瘤标记物', '甲状腺功能', '尿常规',
        '大便常规+隐血', '凝血功能常规', '肝功', '肾功', '电解质', '血脂', '心肌酶',
        '心衰类', '血糖类', '炎症三项', '免疫球蛋白八项', '传染病八项', '糖尿病抗体3项',
        '呼吸道抗体9项', '淋巴细胞亚群', '尿液肾功能7项', '血清胃功能检测')


@pytest.mark.parametrize('name,old_name,code', [
    ('白细胞', '白细胞计数', 'LAB_WBC'),
    ('载脂蛋白A', '载脂蛋白 AI', 'LAB_CATALOG_172'),
    ('载脂蛋白A/B', '载脂蛋白 AI/B', 'LAB_CATALOG_174'),
])
def test_source_name_refresh_keeps_existing_codes_and_aliases(name, old_name, code):
    data = catalog.load_catalog()
    entry = data.match(name, specimen='BLOOD')
    assert entry is not None and (entry.name, entry.code) == (name, code)
    assert data.match(old_name, specimen='BLOOD') == entry


@pytest.mark.parametrize('name,code,category,source_row', [
    ('FSH', 'LAB_CATALOG_030', '性激素6项', 29),
    ('尿液颜色', 'LAB_CATALOG_076', '尿常规', 73),
    ('粪便颜色', 'LAB_CATALOG_099', '大便常规+隐血', 96),
    ('血氨', 'LAB_CATALOG_233', '肝功', 142),
    ('血浆乳酸', 'LAB_LACTATE', '肝功', 143),
    ('甘胆酸', 'LAB_CATALOG_145', '肝功', 144),
    ('尿素氮', 'LAB_CATALOG_231', '肾功', 155),
    ('尿肌酐', 'LAB_CATALOG_235', '尿液肾功能7项', 229),
    ('尿α2-微球蛋白', 'LAB_CATALOG_238', '尿液肾功能7项', 232),
    ('尿白蛋白/肌酐比值', 'LAB_UACR', '尿液肾功能7项', 236),
    ('幽门螺旋杆菌抗体', 'LAB_CATALOG_247', '血清胃功能检测', 238),
    ('PGI', 'LAB_CATALOG_061', '血清胃功能检测', 239),
    ('PGII', 'LAB_CATALOG_062', '血清胃功能检测', 240),
])
def test_reorganized_rows_keep_the_existing_indicator_identity(name, code, category, source_row):
    entry = catalog.load_catalog().match(name)
    assert entry is not None
    assert (entry.code, entry.category, entry.source_rows) == (code, category, (source_row,))


def test_confirmed_lymphocyte_names_keep_distinct_counts_and_unchanged_ranges():
    data = catalog.load_catalog()
    total, t_cells = data.match('淋巴细胞总数'), data.match('总T淋巴细胞数')
    assert (total.code, t_cells.code) == ('LAB_LYMPH_TOTAL_COUNT', 'LAB_TOTAL_T_COUNT')
    assert t_cells.name == '总T淋巴细胞数'
    percent, count = data.match('总B淋巴细胞百分比'), data.match('总B淋巴细胞数量')
    assert percent.name == '总B淋巴细胞百分比'
    assert count.name == '总B淋巴细胞数量'
    assert (percent.reference_for().label, percent.unit) == ('90–323', '%')
    assert (count.reference_for().label, count.unit) == ('5–18', '10^6/L')


def test_removed_stomach_entries_are_not_mapped_to_remaining_indicators():
    data = catalog.load_catalog()
    assert data.match('胃蛋白酶原Ⅰ/Ⅱ') is None
    assert data.match('筛查评分') is None


def test_historical_blood_count_names_keep_crp_matching():
    data = catalog.load_catalog()
    entry = data.match('CRP', panel='血常规')
    assert entry is not None
    assert entry == data.match('CRP', panel='血常规（急诊）')
    assert entry == data.match('CRP', panel='CBC')
    assert entry.reference_for().label == '0–6'


@pytest.mark.parametrize('sampled,expected', [
    (date(2026, 9, 21), 5), (date(2026, 9, 22), 6), (date(2026, 9, 23), 6),
    (date(2019, 1, 1), None), (None, None),
])
def test_completed_age_at_sampling(sampled, expected):
    assert catalog.age_on(date(2020, 9, 22), sampled) == expected
    assert catalog.age_on(None, sampled) is None


def test_alt_aliases_use_routine_definition():
    data = catalog.load_catalog()
    alt = data.match('ALT')
    assert alt is not None
    assert all(data.match(name) == alt for name in ('谷丙转氨酶', '丙氨酸氨基转移酶'))
    assert alt.category == '肝功'
    assert alt.unit == 'U/L'
    assert alt.reference_for(sex='F').label == '7–40'
    assert alt.reference_for(sex='M').label == '9–50'
    assert alt.reference_for() is None


@pytest.mark.parametrize('name,sex,age,label', [
    ('泌乳素', 'F', 50, '70.81–566.46'), ('泌乳素', 'F', 51, '58.09–416.37'),
    ('泌乳素', 'M', 50, '55.97–278.36'),
    ('磷', '', 5, '1.29–2.26'), ('磷', '', 6, '0.85–1.51'),
    ('磷', '', 100, '0.85–1.51'), ('磷', '', 101, None),
    ('碱性磷酸酶', 'M', 15, '40–390'), ('碱性磷酸酶', 'M', 16, '45–125'),
    ('碱性磷酸酶', 'F', 49, '35–100'), ('碱性磷酸酶', 'F', 50, '50–135'),
    ('碱性磷酸酶', 'M', 101, None), ('碱性磷酸酶', '', 16, None),
])
def test_confirmed_reference_boundaries(name, sex, age, label):
    entry = catalog.load_catalog().match(name)
    reference = entry.reference_for(sex=sex, birth_date=date(2026-age, 9, 22), sampled_on=date(2026, 9, 22))
    assert (reference.label if reference else None) == label


def test_colors_need_specimen_and_unknown_names_remain_unmapped():
    data = catalog.load_catalog()
    assert data.match('颜色') is None
    urine = data.match('颜色', specimen='URINE')
    stool = data.match('颜色', panel='大便常规')
    assert urine.name == '尿液颜色' and stool.name == '粪便颜色'
    assert urine.code != stool.code
    assert urine.reference_for().label == '淡黄色'
    assert data.match('ALT相关新项目') is None
    assert data.match('白細胞神秘指标') is None


def test_percent_and_count_are_not_aliases():
    data = catalog.load_catalog()
    assert data.match('中性粒细胞百分数').code != data.match('中性粒细胞绝对数').code


def test_stages_require_explicit_per_result_context():
    entry = catalog.load_catalog().match('促卵泡生成激素')
    assert entry.reference_for(sex='F', phase='卵泡期').label == '3.85–8.78'
    assert entry.reference_for(sex='F', phase='黄体期').label == '1.79–5.12'
    assert entry.reference_for(sex='F') is None
    assert entry.reference_for(sex='M', phase='卵泡期') is None
    assert entry.reference_for(sex='F', phase='卵泡期、黄体期') is None


def test_uncovered_and_qualitative_ranges():
    data = catalog.load_catalog()
    assert data.match('肾小球滤过率-GFR').reference_for() is None
    reference = data.match('尿葡萄糖').reference_for()
    assert reference.label == '阴性'
    assert reference.compare('阳性') == 'different'
    assert reference.compare('阴性') == 'within'
    assert reference.compare('123') == 'unavailable'


def test_exact_dimension_conversion_and_unknown_units():
    entry = catalog.load_catalog().match('血红蛋白')
    assert entry.standardize('13.2', 'g/dL').value == Decimal('132')
    assert entry.standardize('13.2', 'g/dL').unit == 'g/L'
    assert entry.standardize('132', 'g/L').value == Decimal('132')
    unknown = entry.standardize('13.2', 'mmol/L')
    assert unknown.value is None
    assert unknown.display_value == '13.2' and unknown.unit == 'mmol/L'
    assert not unknown.reliable
    assert not entry.standardize('132', '').reliable


@pytest.mark.parametrize('unit', [
    '10⁹/L', '×10^9/L', 'X10^9/L', 'x10^9/L', '109/L', '×109/L', 'X109/L', 'x109/L',
    '×10⁹/L', 'X10⁹/L', ' Ｘ 10^9 / l ',
])
@pytest.mark.parametrize('raw_value', ['5.20', '<5.20'])
def test_count_unit_multiplication_prefix_preserves_value_without_conversion(unit, raw_value):
    entry = catalog.load_catalog().match('白细胞', specimen='BLOOD')
    standardized = entry.standardize(raw_value, unit)
    assert standardized.reliable and not standardized.converted
    assert standardized.display_value == raw_value and standardized.unit == '10^9/L'
    assert standardized.value == (Decimal('5.20') if raw_value == '5.20' else None)
    assert catalog.unit_key(unit) == '10^9/L'
    assert _unit_key(unit) == _unit_key('10^9/L')


@pytest.mark.parametrize('unit', ['XX10^9/L', 'Xmg/L', '1109/L', '109/', '109/L5-10'])
def test_ambiguous_count_notation_is_not_treated_as_an_equivalent_unit(unit):
    entry = catalog.load_catalog().match('白细胞', specimen='BLOOD')
    standardized = entry.standardize('5.20', unit)
    assert not standardized.reliable
    assert (standardized.display_value, standardized.unit) == ('5.20', unit)
    assert _unit_key(unit) != _unit_key('10^9/L')


@pytest.mark.parametrize('name,unit', [
    ('白细胞', '109/L'), ('血小板', '×109/L'),
    ('嗜碱性粒细胞计数', 'X109/L'), ('淋巴细胞计数', ' Ｘ １０９ ／ ｌ '),
])
@pytest.mark.parametrize('raw_value', ['5.20', '<5.20'])
def test_flattened_count_alias_preserves_value_for_different_indicators(name, unit, raw_value):
    entry = catalog.load_catalog().match(name, specimen='BLOOD')
    standardized = entry.standardize(raw_value, unit)
    assert standardized.reliable and not standardized.converted
    assert (standardized.display_value, standardized.unit) == (raw_value, '10^9/L')
    assert standardized.value == (Decimal('5.20') if raw_value == '5.20' else None)


@pytest.mark.parametrize('name', ['血红蛋白', '中性粒细胞百分比'])
@pytest.mark.parametrize('unit', ['109/L', '×109/L', 'X109/L'])
def test_count_unit_alias_cannot_convert_to_a_different_dimension(name, unit):
    entry = catalog.load_catalog().match(name, specimen='BLOOD')
    standardized = entry.standardize('5.20', unit)
    assert not standardized.reliable
    assert (standardized.display_value, standardized.unit, standardized.value) == ('5.20', '10^9/L', None)


@pytest.mark.parametrize('unit', ['109/L', '×109/L', 'X109/L'])
def test_count_unit_alias_keeps_scale_when_catalog_requires_a_real_conversion(unit):
    entry = catalog.load_catalog().match('红细胞', specimen='BLOOD')
    standardized = entry.standardize('5.20', unit)
    assert standardized == entry.standardize('5.20', '10^9/L')
    assert standardized.reliable and standardized.converted
    assert (standardized.value, standardized.unit) == (Decimal('0.00520'), '10^12/L')


def test_count_unit_prefix_keeps_exponent_and_volume_scale_distinct():
    entry = catalog.load_catalog().match('白细胞', specimen='BLOOD')
    for unit in ('X10^12/L', '×10^9/mL'):
        standardized = entry.standardize('5.20', unit)
        assert standardized.reliable and standardized.converted
        assert standardized.value == Decimal('5200') and standardized.unit == '10^9/L'
        assert _unit_key(unit) != _unit_key('10^9/L')


@pytest.mark.parametrize('unit,target,expected,converted', [
    ('1012/L', '10^12/L', '5.20', False),
    ('×1012/ML', '10^12/mL', '5.20', False),
    ('X106/L', '10^6/L', '5.20', False),
    ('10⁶/ML', '10^6/mL', '5.20', False),
    ('109', '10^9/mL', '5.20', False),
    ('X1012', '10^12/L', '5.20', False),
    ('109', '10^12/L', '0.0052', True),
    ('×109', '10^12/mL', '0.0052', True),
    ('×109/ML', '10^9/L', '5200', True),
    ('109/mL', '10^9/mL', '5.20', False),
    ('X109/ML', '10^12/L', '5.2', True),
    ('106', '/μL', '5200000', True),
])
@pytest.mark.parametrize('prefix', ['', '<'])
def test_count_ocr_uses_catalog_target_without_losing_exponent_or_volume(unit, target, expected, converted, prefix):
    entry = replace(catalog.load_catalog().match('白细胞', specimen='BLOOD'), unit=target)
    value = entry.standardize(prefix + '5.20', unit)
    assert value.reliable and value.converted == converted
    assert (value.display_value, value.unit) == (prefix + expected, target)
    assert value.value == (None if prefix else Decimal(expected))


@pytest.mark.parametrize('unit', ['109', '×1012'])
def test_count_without_denominator_cannot_borrow_a_different_dimension(unit):
    value = catalog.load_catalog().match('血红蛋白').standardize('109', unit)
    assert not value.reliable and not value.converted
    assert value.display_value == '109' and value.value is None


@pytest.mark.parametrize('unit,expected', [('Umo1/L', 'μmol/L'), ('Mmo1/L', 'mmol/L'),
                                         ('umol/L', 'μmol/L'), ('ug/L', 'μg/L'), ('uL', 'μL'), ('µL', 'μL')])
def test_spelling_is_normalized_even_when_catalog_dimension_is_incompatible(unit, expected):
    value = catalog.load_catalog().match('白细胞', specimen='BLOOD').standardize('12.30', unit)
    assert not value.reliable and value.value is None
    assert (value.display_value, value.unit) == ('12.30', expected)


@pytest.mark.parametrize('name,unit,canonical_unit', [
    ('肌酐', 'umo1/L', 'umol/L'),
    ('肌酐', 'μmo1/L', 'μmol/L'),
    ('肌酐', 'µmo1/L', 'μmol/L'),
    ('肌酐', 'ｕｍｏ１／ｌ', 'umol/L'),
    ('肌酐', ' u mo1 / l ', 'umol/L'),
    ('尿素', 'mmo1/L', 'mmol/L'),
    ('尿素', 'ｍｍｏ１／Ｌ', 'mmol/L'),
    ('肌酐', 'Umo1/L', 'μmol/L'),
    ('尿素', 'Mmo1/L', 'mmol/L'),
])
@pytest.mark.parametrize('raw_value', ['12.30', '<12.30'])
def test_molar_unit_ocr_digit_preserves_value_without_conversion(name, unit, canonical_unit, raw_value):
    entry = catalog.load_catalog().match(name)
    standardized = entry.standardize(raw_value, unit)
    assert standardized.reliable and not standardized.converted
    assert (standardized.display_value, standardized.unit) == (raw_value, entry.unit)
    assert standardized.value == (Decimal('12.30') if raw_value == '12.30' else None)
    assert _unit_key(unit) == _unit_key(canonical_unit)


@pytest.mark.parametrize('unit', [
    'umo1/', 'umo1/L 5.1--19', 'umo1/L1.7--6.8', 'um01/L', 'umol/1',
])
def test_incomplete_or_ambiguous_molar_unit_is_not_automatically_corrected(unit):
    entry = catalog.load_catalog().match('肌酐')
    standardized = entry.standardize('12.30', unit)
    assert not standardized.reliable
    assert (standardized.display_value, standardized.unit) == ('12.30', unit)
    assert _unit_key(unit) not in {_unit_key('umol/L'), _unit_key('mmol/L')}


@pytest.mark.parametrize('unit', ['mmol/L', 'mmo1/L', 'Mmo1/L'])
def test_molar_unit_ocr_digit_keeps_micro_and_milli_scales_distinct(unit):
    entry = catalog.load_catalog().match('肌酐')
    standardized = entry.standardize('0.1', unit)
    assert standardized.reliable and standardized.converted
    assert (standardized.value, standardized.unit) == (Decimal('100'), 'μmol/L')
    assert _unit_key('mmo1/L') != _unit_key('umo1/L')


def test_conversion_does_not_casefold_different_si_prefixes():
    entry = catalog.load_catalog().match('泌乳素')
    assert entry.standardize('100', 'mIU/L').value == Decimal('100')
    assert not entry.standardize('100', 'MIU/L').reliable


@pytest.mark.parametrize('name', ['HCT', 'NEUT%'])
def test_missing_percentage_unit_is_not_an_explicit_ratio(name):
    entry = catalog.load_catalog().match(name)
    missing = entry.standardize('40', '')
    assert (missing.display_value, missing.unit, missing.value) == ('40', '%', Decimal('40'))
    assert missing.reliable and not missing.converted
    explicit = entry.standardize('0.4', 'L/L')
    assert (explicit.display_value, explicit.unit, explicit.value) == ('40', '%', Decimal('40'))
    assert explicit.reliable and explicit.converted


@pytest.mark.parametrize('raw_value', ['0.4', '<0.4'])
def test_blank_percentage_unit_preserves_numeric_scale_and_comparator(raw_value):
    value = catalog.load_catalog().match('HCT').standardize(raw_value, '  ')
    assert value.reliable and not value.converted
    assert (value.display_value, value.unit) == (raw_value, '%')


def test_dimensionless_indicator_keeps_its_empty_unit():
    entry = catalog.load_catalog().match('尿葡萄糖')
    value = entry.standardize('阴性', '')
    assert value.reliable and not value.converted
    assert (value.display_value, value.unit) == ('阴性', '')


def test_numeric_boundary_and_non_numeric_results():
    entry = catalog.load_catalog().match('ALT')
    reference = entry.reference_for(sex='F')
    assert reference.compare('6.9') == 'below'
    assert reference.compare('7') == 'within'
    assert reference.compare('40') == 'within'
    assert reference.compare('40.1') == 'above'
    assert reference.compare('溶血') == 'unavailable'
    assert reference.compare('NaN') == 'unavailable'


@pytest.mark.parametrize('raw,expected,status', [
    ('<7', '<70', 'below'), ('≥20', '≥200', 'above'), ('≤20', '≤200', 'unavailable'),
])
def test_bounded_values_convert_without_becoming_exact_numbers(raw, expected, status):
    entry = catalog.load_catalog().match('血红蛋白')
    standardized = entry.standardize(raw, 'g/dL')
    assert standardized.display_value == expected
    assert standardized.unit == 'g/L' and standardized.reliable and standardized.converted
    assert standardized.value is None
    assert entry.reference_for(sex='M').compare(standardized.display_value) == status


@pytest.mark.parametrize('name,value,specimen', [
    ('颜色', '淡黄色', 'URINE'), ('颜色', '黄色', 'STOOL'),
    ('透明度', '清晰', 'URINE'), ('性状', '软便', 'STOOL'),
])
def test_source_qualitative_results_are_extracted_without_numeric_coercion(name, value, specimen):
    from apps.labs.extraction import extract_observations
    from apps.labs.dictionary import phase_two_dictionary
    from apps.labs.models import ResultType
    from tests.labs.test_extraction import _page, _region
    page = _page(_region('标本：' + ('尿液' if specimen == 'URINE' else '粪便'), .05, .3, 0, top=.05, bottom=.08),
                 _region(name, .05, .3, 1), _region(value, .4, .6, 2))
    row, = extract_observations((page,), phase_two_dictionary())
    assert row.raw_value == value and row.result_type == ResultType.QUALITATIVE
    assert not any(item['code'] == 'recognition_uncertain' for item in row.quality_issues)
    entry = catalog.load_catalog().match(row.raw_name, specimen=row.specimen)
    assert entry.reference_for().label == value


@pytest.mark.parametrize('panel,expected', [('尿常规', '尿液颜色'), ('大便常规', '粪便颜色')])
def test_color_panel_context_is_retained_without_inventing_specimen(panel, expected):
    from apps.labs.extraction import extract_observations
    from apps.labs.dictionary import phase_two_dictionary
    from tests.labs.test_extraction import _page, _region
    page = _page(_region(panel, .05, .3, 0, top=.05, bottom=.08),
                 _region('颜色', .05, .3, 1), _region('黄色', .4, .6, 2))
    row, = extract_observations((page,), phase_two_dictionary())
    assert row.specimen == ''
    entry = catalog.load_catalog().match(row.raw_name, panel=row.field_evidence['panel']['value'])
    assert entry.name == expected


def test_pct_uses_explicit_panel_to_distinguish_different_indicators():
    data = catalog.load_catalog()
    assert data.match('PCT', specimen='BLOOD') is None
    assert data.match('PCT', panel='CBC').code == 'LAB_PLATELETCRIT'
    assert data.match('PCT', panel='INFLAMMATION').code == 'LAB_PCT'


@pytest.mark.parametrize('name,abbreviation,expected', [
    ('免疫球蛋白 IgG', 'IgG', '免疫球蛋白 IgG'),
    ('免疫球蛋白 IgA', 'IgA', '免疫球蛋白IgA'), ('免疫球蛋白 IgM', 'IgM', '免疫球蛋白IgM'),
    ('补体 C3', 'C3', '补体C3'), ('补体 C4', 'C4', '补体C4'),
])
def test_source_printed_immunology_abbreviations_match_same_indicator(name, abbreviation, expected):
    data = catalog.load_catalog()
    entry = data.match(abbreviation)
    assert entry == data.match(name)
    assert entry.name == expected


@pytest.mark.parametrize('name,first,category,reference', [
    ('肌酐', '急肾功+肝功（急）', '肾功', '41–73'),
    ('胃蛋白酶原Ⅰ', '肿瘤标记物', '血清胃功能检测', '70–160'),
    ('胃蛋白酶原Ⅱ', '肿瘤标记物', '血清胃功能检测', '5–60'),
    ('CRP', '血常规（急诊）', '炎症三项', '0–6'),
])
def test_historical_panels_use_the_new_unique_definition(name, first, category, reference):
    data = catalog.load_catalog()
    a, b = data.match(name, panel=first), data.match(name, panel=category)
    assert a is not None and b is not None
    assert a == b == data.match(name)
    assert a.category == category
    context = dict(sex='F', birth_date=date(2000, 1, 1), sampled_on=date(2026, 9, 22))
    assert a.reference_for(**context).label == reference
    assert data.match(name, panel='不明确分类') == a


@pytest.mark.parametrize('panel,name,category', [
    ('肾功', '肌酐', '肾功'), ('急肾功+肝功（急）', '肌酐', '肾功'),
    ('肿瘤标记物', 'PGI', '血清胃功能检测'), ('血清胃功能检测', 'PGII', '血清胃功能检测'),
    ('血常规（急诊）', 'CRP', '炎症三项'), ('炎症三项', 'CRP', '炎症三项'),
])
def test_historical_report_titles_match_the_new_categories(panel, name, category):
    from apps.labs.extraction import extract_observations
    from apps.labs.dictionary import phase_two_dictionary
    from tests.labs.test_extraction import _page, _region
    page = _page(_region(panel, .05, .8, 0, top=.05, bottom=.08),
                 _region(name, .05, .3, 1), _region('80', .4, .6, 2))
    row, = extract_observations((page,), phase_two_dictionary())
    entry = catalog.load_catalog().match(row.raw_name, panel=row.field_evidence['panel']['value'])
    assert entry.category == category
