from datetime import date
from decimal import Decimal

import pytest

from apps.labs import catalog


def test_catalog_covers_the_verified_source():
    data = catalog.load_catalog()
    assert data.source_sha256 == 'f03e39f39af3774eb86460d443e292ef85072bbf391ba3625cde53a9744d49e7'
    assert len(data.source_rows) == 208
    assert len(data.groups) == 25


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
    assert alt.category == '肝功-肝细胞损伤'
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


def test_conversion_does_not_casefold_different_si_prefixes():
    entry = catalog.load_catalog().match('泌乳素')
    assert entry.standardize('100', 'mIU/L').value == Decimal('100')
    assert not entry.standardize('100', 'MIU/L').reliable


@pytest.mark.parametrize('name', ['HCT', 'NEUT%'])
def test_missing_percentage_unit_is_not_an_explicit_ratio(name):
    entry = catalog.load_catalog().match(name)
    missing = entry.standardize('40', '')
    assert (missing.display_value, missing.unit, missing.value) == ('40', '', None)
    assert not missing.reliable and not missing.converted
    explicit = entry.standardize('0.4', 'L/L')
    assert (explicit.display_value, explicit.unit, explicit.value) == ('40', '%', Decimal('40'))
    assert explicit.reliable and explicit.converted


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


@pytest.mark.parametrize('name,abbreviation', [
    ('免疫球蛋白 IgG', 'IgG'), ('免疫球蛋白 IgA', 'IgA'), ('免疫球蛋白 IgM', 'IgM'),
    ('补体 C3', 'C3'), ('补体 C4', 'C4'),
])
def test_source_printed_immunology_abbreviations_match_same_indicator(name, abbreviation):
    data = catalog.load_catalog()
    assert data.match(abbreviation) == data.match(name)


@pytest.mark.parametrize('name,first,second,first_range,second_range', [
    ('肌酐', '肾功', '急肾功+肝功（急）', '41–73', '44–133'),
    ('胃蛋白酶原Ⅰ', '肿瘤标记物', '血清胃功能检测', '70–160', '70–165'),
    ('胃蛋白酶原Ⅱ', '肿瘤标记物', '血清胃功能检测', '5–60', '3–15'),
    ('CRP', '血常规（急诊）', '炎症三项', '0–6', '0–6'),
])
def test_group_variants_keep_one_indicator_and_select_report_range(name, first, second, first_range, second_range):
    data = catalog.load_catalog()
    a, b = data.match(name, panel=first), data.match(name, panel=second)
    assert a is not None and b is not None
    assert a.code == b.code
    assert (a.category, b.category) == (first, second)
    context = dict(sex='F', birth_date=date(2000, 1, 1), sampled_on=date(2026, 9, 22))
    assert a.reference_for(**context).label == first_range
    assert b.reference_for(**context).label == second_range
    assert data.match(name) is None
    assert data.match(name, panel='不明确分类') is None


@pytest.mark.parametrize('panel,name', [
    ('肾功', '肌酐'), ('急肾功+肝功（急）', '肌酐'),
    ('肿瘤标记物', 'PGI'), ('血清胃功能检测', 'PGII'),
    ('血常规（急诊）', 'CRP'), ('炎症三项', 'CRP'),
])
def test_group_variant_report_title_is_retained_by_extraction(panel, name):
    from apps.labs.extraction import extract_observations
    from apps.labs.dictionary import phase_two_dictionary
    from tests.labs.test_extraction import _page, _region
    page = _page(_region(panel, .05, .8, 0, top=.05, bottom=.08),
                 _region(name, .05, .3, 1), _region('80', .4, .6, 2))
    row, = extract_observations((page,), phase_two_dictionary())
    entry = catalog.load_catalog().match(row.raw_name, panel=row.field_evidence['panel']['value'])
    assert entry.category == panel
