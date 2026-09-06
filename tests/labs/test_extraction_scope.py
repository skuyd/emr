"""Synthetic regressions for report sections mistaken for laboratory tables."""

import pytest

from apps.labs.candidates import extract_lab_candidates
from apps.labs.dictionary import load_dictionary
from apps.labs.extraction import extract_observations
from apps.labs.models import CapabilityLevel
from apps.processing.value_objects import OcrPage, OcrRegion


@pytest.fixture
def dictionary():
    return load_dictionary('apps/labs/dictionaries/phase-two.json')


def page(rows, number=1):
    regions = []
    for y, cells in rows:
        for x, text in cells:
            width = .012 if len(text) == 1 else .07
            regions.append(OcrRegion(text, ((x, y), (x + width, y),
                (x + width, y + .025), (x, y + .025)), .99, len(regions)))
    return OcrPage(number, 1000, 1400, tuple(regions), 'synthetic', '1')


HEADERS = [(.05, '项目'), (.4, '结果'), (.7, '单位')]
HGB = [(.05, '血红蛋白'), (.4, '130'), (.7, 'g/L')]


def test_fragmented_prose_cannot_create_lab_items_but_same_page_rows_survive(dictionary):
    # A PDF text layer can expose individual glyphs, including the aliases K/P.
    glyphs = [(i * .025 + .05, ch) for i, ch in enumerate('VAR K 4 R 2 S 1 P 3'.replace(' ', ''))]
    source = page([(.1, glyphs), (.3, HGB),
                   (.4, [(.05, 'INR'), (.4, '1.02')])])
    items = extract_observations((source,), dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [('血红蛋白', '130'), ('INR', '1.02')]
    assert [x.raw_name for x in extract_lab_candidates((source,), 'a' * 64)] == ['INR', '血红蛋白']


@pytest.mark.parametrize('heading', [
    '基因检测报告', '基因变异总览', '病理诊断报告书', '超声检查报告单',
    '入院记录', '病程记录', '长期医嘱单', '参考文献',
])
def test_non_lab_sections_do_not_emit_observations_or_dictionary_candidates(dictionary, heading):
    source = page([(.05, [(.05, heading)]),
                   (.2, [(.05, '合成条目'), (.4, '12'), (.7, '%')])])
    assert extract_observations((source,), dictionary) == ()
    assert extract_lab_candidates((source,), 'b' * 64) == ()


def test_explicit_lab_table_reopens_within_a_non_lab_page(dictionary):
    source = page([(.05, [(.05, '基因变异总览')]),
                   (.1, [(.05, '变异条目'), (.4, '12')]),
                   (.3, HEADERS), (.4, HGB),
                   (.5, [(.05, '未收录检验项目'), (.4, '7.5'), (.7, 'U/L')])])
    items = extract_observations((source,), dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [('血红蛋白', '130'), ('未收录检验项目', '7.5')]
    assert items[1].capability_level == CapabilityLevel.SEARCH_ONLY
    assert {x.raw_name for x in extract_lab_candidates((source,), 'c' * 64)} == {'血红蛋白', '未收录检验项目'}


def test_generic_name_result_headers_do_not_reopen_a_non_lab_section(dictionary):
    source = page([(.05, [(.05, '基因检测报告')]),
                   (.1, [(.05, '项目'), (.4, '结果')]),
                   (.2, [(.05, '变异条目'), (.4, '12')])])
    assert extract_observations((source,), dictionary) == ()


@pytest.mark.parametrize('heading', ['备注', '结果说明', '参考文献'])
def test_table_ends_at_a_new_explanatory_section_and_can_restart(dictionary, heading):
    source = page([(.1, HEADERS), (.2, HGB),
                   (.3, [(.05, heading)]),
                   (.4, [(.05, '文献编号'), (.4, '12')]),
                   (.5, HEADERS), (.6, HGB)])
    assert [x.raw_value for x in extract_observations((source,), dictionary)] == ['130', '130']
    assert len(extract_lab_candidates((source,), 'd' * 64)) == 2


def test_ending_one_table_does_not_suppress_the_adjacent_lab_table(dictionary):
    source = page([
        (.1, [(.03, '项目'), (.2, '结果'), (.32, '单位'),
              (.53, '项目'), (.7, '结果'), (.82, '单位')]),
        (.2, [(.03, '血红蛋白'), (.2, '130'), (.32, 'g/L'),
              (.53, '血红蛋白'), (.7, '131'), (.82, 'g/L')]),
        (.3, [(.03, '参考文献')]),
        (.4, [(.03, '文献编号'), (.2, '12'),
              (.53, '血红蛋白'), (.7, '132'), (.82, 'g/L')]),
    ])
    assert [x.raw_value for x in extract_observations((source,), dictionary)] == ['130', '131', '132']


def test_mixed_bundle_keeps_lab_pages_and_resets_a_continued_table_at_non_lab_heading(dictionary):
    pages = (
        page([(.05, [(.05, '入院记录')]), (.2, [(.05, '合成条目'), (.4, '12')])]),
        page([(.1, HEADERS), (.2, HGB)], 2),
        page([(.05, [(.05, '续表')]), (.1, [(.05, '病理诊断报告书')]),
              (.2, [(.05, '染色比例'), (.4, '20'), (.7, '%')])], 3),
        page([(.2, HGB)], 4),
    )
    items = extract_observations(pages, dictionary)
    assert [(x.page_number, x.raw_value) for x in items] == [(2, '130'), (4, '130')]
    assert [x.page for x in extract_lab_candidates(pages, 'e' * 64)] == [2, 4]


def test_headerless_clinical_and_gene_rows_keep_existing_supported_behavior(dictionary):
    source = page([
        (.1, [(.05, 'K'), (.4, '4.2'), (.7, 'mmol/L')]),
        (.2, [(.05, 'INR'), (.4, '1.02')]),
        (.3, [(.05, '尿蛋白'), (.4, '++')]),
        (.4, [(.05, '自定义酶活性'), (.4, '12.3'), (.7, 'U/L')]),
        (.5, [(.05, 'BRCA1'), (.4, '未检出')]),
    ])
    items = extract_observations((source,), dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [
        ('K', '4.2'), ('INR', '1.02'), ('尿蛋白', '++'),
        ('自定义酶活性', '12.3'), ('BRCA1', '未检出'),
    ]
    assert items[-1].standard_code == 'GENE_BRCA1'
    assert items[-1].capability_level == CapabilityLevel.SEARCH_ONLY


@pytest.mark.parametrize(('name', 'value', 'unit', 'code'), [
    ('嗜酸性粒细胞百分比', '2.3', '%', 'LAB_EO_PERCENT'),
    ('凝血酶原时间', '12.0', 's', 'LAB_PT'),
    ('国际标准化比值', '1.02', '', 'LAB_INR'),
])
def test_complete_split_lab_labels_with_short_or_no_unit_are_preserved(dictionary, name, value, unit, code):
    cells = [(.05 + i * .025, glyph) for i, glyph in enumerate(name)]
    cells.append((.4, value))
    if unit:
        cells.append((.7, unit))
    source = page([(.2, cells)])
    item, = extract_observations((source,), dictionary)
    assert (item.standard_code, item.raw_value, item.raw_unit) == (code, value, unit)
    assert [x.raw_name for x in extract_lab_candidates((source,), 'f' * 64)] == [name]


def test_a_slash_in_fragmented_prose_does_not_establish_a_unit_cell(dictionary):
    cells = [(.05 + i * .025, glyph) for i, glyph in enumerate('VARK4R2S1P3')]
    cells.append((.7, 'A/B'))
    assert extract_observations((page([(.2, cells)]),), dictionary) == ()


def test_inline_remark_does_not_end_an_ongoing_table(dictionary):
    source = page([(.1, HEADERS), (.2, HGB),
                   (.3, [(.05, '备注：以下项目另附说明。')]), (.4, HGB)])
    assert [x.raw_value for x in extract_observations((source,), dictionary)] == ['130', '130']


@pytest.mark.parametrize('with_unit', [True, False])
def test_active_adjacent_table_can_update_its_headers_without_reopening_ended_table(dictionary, with_unit):
    new_headers = [(.53, '项目'), (.7 if with_unit else .82, '结果')]
    next_row = [(.03, '文献编号'), (.2, '12'), (.53, 'INR'), (.7 if with_unit else .82, '1.02')]
    if with_unit:
        new_headers.append((.82, '单位'))
    source = page([
        (.1, [(.03, '项目'), (.2, '结果'), (.32, '单位'),
              (.53, '项目'), (.7, '结果'), (.82, '单位')]),
        (.2, [(.03, '血红蛋白'), (.2, '130'), (.32, 'g/L'),
              (.53, '血红蛋白'), (.7, '131'), (.82, 'g/L')]),
        (.3, [(.03, '参考文献')]), (.4, new_headers), (.5, next_row),
    ])
    items = extract_observations((source,), dictionary)
    assert [(x.raw_name, x.raw_value) for x in items] == [('血红蛋白', '130'), ('血红蛋白', '131'), ('INR', '1.02')]


def test_new_table_after_an_ended_section_cannot_inherit_old_specimen_or_panel(dictionary):
    source = page([(.05, [(.05, '标本：全血'), (.4, '血常规')]),
                   (.1, HEADERS), (.2, HGB), (.3, [(.05, '参考文献')]),
                   (.4, HEADERS), (.5, [(.05, 'PCT'), (.4, '0.2'), (.7, '%')])])
    first, second = extract_observations((source,), dictionary)
    assert first.specimen == 'BLOOD'
    assert second.specimen == ''
    assert second.standard_code.startswith('CANDIDATE_')
    assert 'specimen' not in second.field_evidence


def test_updating_right_headers_keeps_left_header_confidence_restrictions(dictionary):
    from dataclasses import replace

    headers = [(.03, '项目'), (.2, '结果'), (.32, '单位'),
               (.53, '项目'), (.7, '结果'), (.82, '单位')]
    values = [(.03, '血红蛋白'), (.2, '130'), (.32, 'g/L'),
              (.53, '血红蛋白'), (.7, '131'), (.82, 'g/L')]
    source = page([(.1, headers), (.2, values), (.3, headers[3:]), (.4, values)])
    source = replace(source, regions=tuple(replace(x, confidence=.2) if i < 3 else x
                                         for i, x in enumerate(source.regions)))
    items = extract_observations((source,), dictionary)
    assert items[0].capability_level == CapabilityLevel.SEARCH_ONLY
    assert items[2].capability_level == CapabilityLevel.SEARCH_ONLY
    assert 'association_conflict' in {x['code'] for x in items[2].quality_issues}
    assert items[3].capability_level == CapabilityLevel.STABLE


def test_reopening_right_lab_table_does_not_forget_rejected_left_section(dictionary):
    source = page([
        (.05, [(.03, '基因检测报告')]),
        (.1, [(.03, '项目'), (.2, '结果'),
              (.53, '项目'), (.7, '结果'), (.82, '单位')]),
        (.2, [(.03, '变异条目'), (.2, '12'),
              (.53, '血红蛋白'), (.7, '131'), (.82, 'g/L')]),
        (.3, [(.03, '项目'), (.2, '结果')]),
        (.4, [(.03, '变异条目'), (.2, '13')]),
        (.5, [(.53, '血红蛋白'), (.7, '132'), (.82, 'g/L')]),
    ])
    assert [(x.raw_name, x.raw_value) for x in extract_observations((source,), dictionary)] == [
        ('血红蛋白', '131'), ('血红蛋白', '132'),
    ]
