from datetime import date
from decimal import Decimal
import re

import pytest

from apps.labs.comparison import comparable_cell, comparison_view
from apps.labs.readmodels import effective_rows
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_phase_two_comparison import row as make_row


pytestmark = pytest.mark.django_db


def indicator(patient, *, name='ALT', code='LAB_ALT', value='45', unit='U/L', reference='0-99', **kwargs):
    _, row = make_row(patient, value=value, **kwargs)
    row.raw_name, row.standard_name, row.standard_code = name, name, code
    row.raw_unit, row.reference_range_raw, row.report_flag_raw = unit, reference, 'L'
    row.save()
    return row


def test_catalog_range_overrides_report_but_preserves_original(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-standard')
    patient.sex = 'F'
    patient.save()
    original = indicator(patient)
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.standard_reference == '7–40'
    assert cell.abnormal.status == 'above'
    assert cell.abnormal.symbol == '↑'
    assert cell.observation.reference_range_raw == '0-99'
    assert cell.observation.report_flag_raw == 'L'
    original.refresh_from_db()
    assert original.raw_value == '45' and original.raw_unit == 'U/L'


def test_missing_patient_condition_hides_reference_and_report_flag(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-missing')
    indicator(patient)
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.standard_reference == ''
    assert cell.abnormal.symbol == '' and cell.abnormal.label == ''
    assert cell.display_value == '45'


def test_reliable_conversion_changes_value_before_comparison(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-convert')
    patient.sex = 'F'
    patient.save()
    indicator(patient, name='血红蛋白', code='LAB_HGB', value='16', unit='g/dL', reference='10-99')
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.display_value == '160' and cell.unit == 'g/L'
    assert cell.numeric_value == Decimal('160')
    assert cell.abnormal.status == 'above'
    assert cell.observation.raw_value == '16' and cell.observation.raw_unit == 'g/dL'


def test_unknown_unit_keeps_original_and_never_uses_standard_flag(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-unit-unknown')
    patient.sex = 'F'
    patient.save()
    indicator(patient, value='16', unit='unrecognized')
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.display_value == '16' and cell.unit == 'unrecognized'
    assert cell.abnormal.symbol == ''
    assert not cell.plot_eligible and not cell.trend_eligible
    assert cell.review_required


def test_standardization_keeps_result_reliability_gate(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-review-gate')
    patient.sex = 'F'
    patient.save()
    original = indicator(patient)
    original.quality_issues = [{'code': 'recognition_uncertain', 'fields': ['raw_value']}]
    original.save()
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.standard_reference == '7–40'
    assert cell.abnormal.symbol == '' and not cell.trend_eligible
    assert cell.review_required


def test_patient_correction_reselects_historical_range(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-history')
    indicator(patient, name='磷', code='LAB_P', value='1', unit='mmol/L', day=date(2026, 9, 21))
    patient.birth_date = date(2020, 9, 22)
    patient.save()
    first = comparable_cell(effective_rows(patient)[0])
    assert first.standard_reference == '1.29–2.26'
    patient.birth_date = date(2019, 9, 22)
    patient.save()
    second = comparable_cell(effective_rows(patient)[0])
    assert second.standard_reference == '0.85–1.51'


def test_alias_history_is_one_row_and_uncataloged_items_are_other(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-history-alias')
    for day, name in enumerate(('丙氨酸氨基转移酶', '谷丙转氨酶', 'ALT'), 1):
        indicator(patient, name=name, day=date(2026, 9, day))
    indicator(patient, name='表外项目', code='CANDIDATE_OUTSIDE')
    comparison = comparison_view(patient)
    alt = next(row for row in comparison.rows if row.standard_code == 'LAB_ALT')
    assert alt.standard_name == '丙氨酸氨基转移酶'
    assert alt.category == '肝功'
    assert sum(len(cells) for cells in alt.cells) == 3
    assert any(row.standard_name == '表外项目' and row.category == 'OTHER' for row in comparison.rows)


@pytest.mark.parametrize('name,printed,code,unit,category', [
    ('单核细胞百分比', '3★MONO%单核细胞百分比', 'LAB_MONO_PERCENT', '%', '血常规'),
    ('丙氨酸氨基转移酶', '4 ★ALT 丙氨酸氨基转移酶', 'LAB_ALT', 'U/L', '肝功'),
    ('钠', '5 ★NA 钠', 'LAB_NA', 'mmol/L', '电解质'),
])
def test_printed_alias_history_uses_only_its_catalog_category(
        django_user_model, name, printed, code, unit, category):
    client, patient = _patient(django_user_model, 'catalog-printed-' + code)
    indicator(patient, name=name, code=code, unit=unit, day=date(2026, 9, 20))
    original = indicator(patient, name=printed, code=code, unit=unit, day=date(2026, 9, 21))
    original.specimen = ''
    original.save(update_fields=['specimen'])
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    view = response.context['comparison']
    history, = view.rows
    assert history.category == category
    assert [group.label for group in view.groups] == [category]
    assert [group['category'] for group in view.selection_groups] == [category]
    cells = [cell for entries in history.cells for cell in entries]
    assert len(cells) == 2
    assert all(cell.catalog and cell.catalog.indicator.code == code for cell in cells)
    original.refresh_from_db()
    assert (original.raw_name, original.raw_value, original.raw_unit, original.specimen) == (
        printed, '45', unit, '')


@pytest.mark.parametrize('name,specimen', [
    ('3 ★ALT 未知项目', ''),
    ('4 ★ALT AST', ''),
    ('5 ★颜色', ''),
    ('6 ★ALT', 'URINE'),
    ('7 ★ALT 5', ''),
])
def test_printed_names_do_not_map_unknown_conflicting_or_ambiguous_results(
        django_user_model, name, specimen):
    _, patient = _patient(django_user_model, 'catalog-printed-unresolved')
    original = indicator(patient, name=name)
    original.specimen = specimen
    original.save(update_fields=['specimen'])
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.catalog is None


@pytest.mark.parametrize('name,old_name,code,unit', [
    ('白细胞', '白细胞计数', 'LAB_WBC', '10^9/L'),
    ('载脂蛋白A', '载脂蛋白 AI', 'LAB_CATALOG_172', 'g/L'),
    ('载脂蛋白A/B', '载脂蛋白 AI/B', 'LAB_CATALOG_174', ''),
])
def test_renamed_catalog_items_keep_old_and_new_report_names_in_one_history(
        django_user_model, name, old_name, code, unit):
    _, patient = _patient(django_user_model, 'catalog-renamed-' + code)
    originals = [indicator(patient, name=raw_name, code=code, unit=unit, day=date(2026, 9, day))
                 for day, raw_name in enumerate((old_name, name), 20)]
    history, = comparison_view(patient).rows
    assert (history.standard_name, history.standard_code) == (name, code)
    assert {source.pk for cells in history.cells for cell in cells for source in cell.sources} == {row.pk for row in originals}
    for row, raw_name in zip(originals, (old_name, name)):
        row.refresh_from_db()
        assert row.raw_name == raw_name and row.standard_code == code


def test_page_displays_standard_value_and_original_details(django_user_model):
    client, patient = _patient(django_user_model, 'catalog-page')
    patient.sex = 'F'
    patient.save()
    row = indicator(patient, name='血红蛋白', code='LAB_HGB', value='16', unit='g/dL')
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    html = response.content.decode()
    assert '标准参考范围' in html and '115–150' in html
    assert '>160</a>' in html
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True).content.decode()
    assert '标准参考范围' in detail and '115–150' in detail
    assert 'value="16"' in detail and 'value="g/dL"' in detail and 'value="0-99"' in detail


@pytest.mark.parametrize('name,code,unit,expected', [
    ('白细胞计数', 'LAB_WBC', '10^9/L', '3.5–9.5'),
    ('血红蛋白', 'LAB_HGB', 'g/L', '115–150'),
    ('睾酮', 'LAB_CATALOG_045', 'nmol/L', '0.35–2.6'),
    ('泌乳素', 'LAB_CATALOG_046', 'mIu/L', '70.81–566.46'),
])
def test_non_phase_references_appear_once_under_indicator_not_in_results(
        django_user_model, name, code, unit, expected):
    client, patient = _patient(django_user_model, 'catalog-reference-position-' + code)
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    for day in (20, 21):
        observation = indicator(patient, name=name, code=code, unit=unit, day=date(2026, 9, day))
        # A report's phase field does not make an ordinary indicator phase-dependent.
        observation.physiological_phase = '卵泡期'
        observation.save(update_fields=['physiological_phase'])
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    heading = re.search(r'<th scope="row">.*?</th>', html, re.S).group()
    results = re.findall(r'<article>.*?</article>', html, re.S)
    assert f'标准参考范围：{expected} {unit}' in heading
    assert heading.count('class="comparison-reference"') == 1
    assert len(results) == 2
    assert all('标准参考范围：' not in result for result in results)


def test_age_based_reference_ranges_are_dated_and_filtered_under_indicator(django_user_model):
    client, patient = _patient(django_user_model, 'catalog-reference-age-position')
    patient.birth_date = date(2020, 9, 22)
    patient.save()
    for day in (21, 22):
        indicator(patient, name='磷', code='LAB_P', unit='mmol/L', day=date(2026, 9, day))
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    heading = re.search(r'<th scope="row">.*?</th>', html, re.S).group()
    assert '标准参考范围：1.29–2.26 mmol/L' in heading and '2026-09-21' in heading
    assert '标准参考范围：0.85–1.51 mmol/L' in heading and '2026-09-22' in heading
    assert all('标准参考范围：' not in result for result in re.findall(r'<article>.*?</article>', html, re.S))
    filtered = client.get('/labs/compare/', {'patient': patient.pk, 'end': '2026-09-21'}).content.decode()
    heading = re.search(r'<th scope="row">.*?</th>', filtered, re.S).group()
    assert '1.29–2.26' in heading and '0.85–1.51' not in heading
    assert 'comparison-reference-date' not in heading


def test_phase_references_stay_with_each_result_and_missing_phase_stays_hidden(django_user_model):
    client, patient = _patient(django_user_model, 'catalog-phase-reference-position')
    patient.sex = 'F'
    patient.save()
    for day, phase in enumerate(('卵泡期', '黄体期', ''), 20):
        observation = indicator(patient, name='FSH', code='LAB_CATALOG_030', unit='IU/L', day=date(2026, 9, day))
        observation.physiological_phase = phase
        observation.save(update_fields=['physiological_phase'])
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    heading = re.search(r'<th scope="row">.*?</th>', html, re.S).group()
    results = re.findall(r'<article>.*?</article>', html, re.S)
    assert 'comparison-reference' not in heading
    assert len(results) == 3
    assert '卵泡期' in results[0] and '标准参考范围：3.85–8.78 IU/L' in results[0]
    assert '黄体期' in results[1] and '标准参考范围：1.79–5.12 IU/L' in results[1]
    assert '标准参考范围：' not in results[2] and '参考：未提供' not in results[2]


def test_unmatched_range_has_no_missing_placeholder_in_main_table(django_user_model):
    client, patient = _patient(django_user_model, 'catalog-hidden-page')
    indicator(patient, reference='')
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    # Source details may retain the original report's absence; main cells may not.
    import re
    main = re.sub(r'<details class="comparison-sources">.*?</details>', '', html, flags=re.S)
    assert '标准参考范围：' not in main
    assert '暂无参考范围' not in main and '参考：未提供' not in main


def test_conflicting_original_ranges_do_not_override_standard_display(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-original-diff')
    patient.sex = 'F'
    patient.save()
    indicator(patient, reference='0-30')
    indicator(patient, reference='0-99')
    cells = [cell for row in comparison_view(patient).rows for entries in row.cells for cell in entries]
    assert all(cell.abnormal.status == 'above' for cell in cells)
    assert all(cell.standard_reference == '7–40' for cell in cells)


def test_snapshot_preserves_raw_fields_and_standard_values(django_user_model):
    from apps.exports.content import build_snapshot
    _, patient = _patient(django_user_model, 'catalog-snapshot')
    patient.sex = 'F'
    patient.save()
    indicator(patient, name='血红蛋白', code='LAB_HGB', value='16', unit='g/dL')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    lab, = snapshot['labs']
    assert (lab['value'], lab['unit']) == ('160', 'g/L')
    assert (lab['raw_value'], lab['raw_unit'], lab['reference_range_raw']) == ('16', 'g/dL', '0-99')
    assert lab['standard_reference'] == '115–150'
    result, = snapshot['lab_results']
    assert (result['value'], result['standard_reference']) == ('160', '115–150')


@pytest.mark.parametrize('name,code,unit,initial,corrected,before,after', [
    ('ALT', 'LAB_ALT', 'U/L', {'sex': 'F', 'birth_date': '2000-01-01'},
     {'sex': 'M', 'birth_date': '2000-01-01'}, '7–40', '9–50'),
    ('磷', 'LAB_P', 'mmol/L', {'sex': 'F', 'birth_date': '2020-09-22'},
     {'sex': 'F', 'birth_date': '2019-09-22'}, '1.29–2.26', '0.85–1.51'),
])
def test_demographic_correction_invalidates_old_standard_reference_snapshot(
        django_user_model, name, code, unit, initial, corrected, before, after):
    from apps.exports.content import assert_snapshot_current, build_snapshot
    from apps.exports.errors import SnapshotChanged
    client, patient = _patient(django_user_model, 'catalog-snapshot-demographics-' + code)
    assert client.post('/me/demographics/', initial).status_code == 302
    original = indicator(patient, name=name, code=code, unit=unit, day=date(2026, 9, 21))
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert snapshot['labs'][0]['standard_reference'] == before
    assert_snapshot_current(patient, snapshot)
    assert client.post('/me/demographics/', corrected).status_code == 302
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    current = build_snapshot(patient, {'mode': 'all'})
    assert current['labs'][0]['standard_reference'] == after
    assert snapshot['labs'][0]['standard_reference'] == before
    original.refresh_from_db()
    assert (original.raw_value, original.raw_unit, original.reference_range_raw) == ('45', unit, '0-99')


def test_unknown_name_with_legacy_code_does_not_join_catalog_history(django_user_model):
    client, patient = _patient(django_user_model, 'catalog-legacy-name')
    indicator(patient, name='ALT', day=date(2026, 9, 20))
    indicator(patient, name='表外项目', code='LAB_ALT', day=date(2026, 9, 21))
    table = comparison_view(patient)
    assert len(table.rows) == 2
    assert {(row.standard_name, row.category) for row in table.rows} == {
        ('丙氨酸氨基转移酶', '肝功'), ('表外项目', 'OTHER')}
    assert {cell.observation.raw_name for row in table.rows for column in row.cells for cell in column} == {'ALT', '表外项目'}



@pytest.mark.parametrize('name,code,display_name', [
    ('胃蛋白酶原Ⅰ/Ⅱ', 'LAB_CATALOG_246', '胃蛋白酶原I/II'),
    ('筛查评分', 'LAB_CATALOG_248', '筛查评分'),
])
def test_removed_catalog_results_remain_visible_as_other(django_user_model, name, code, display_name):
    from apps.exports.content import build_snapshot
    _, patient = _patient(django_user_model, 'catalog-removed-' + code)
    original = indicator(patient, name=name, code=code, value='9', unit='', reference='旧报告范围')
    original.field_evidence = {**original.field_evidence, 'panel': {'value': '血清胃功能检测'}}
    original.save()
    table = comparison_view(patient, category='OTHER')
    history, = table.rows
    assert (history.standard_name, history.standard_code, history.category) == (display_name, code, 'OTHER')
    cell, = history.cells[0]
    assert cell.catalog is None and cell.standard_reference == ''
    assert cell.observation.pk == original.pk and cell.display_value == '9'
    assert not cell.plot_eligible and not cell.trend_eligible
    lab, = build_snapshot(patient, {'mode': 'all'})['labs']
    assert lab['raw_value'] == '9' and lab['reference_range_raw'] == '旧报告范围'
    original.refresh_from_db()
    assert (original.raw_name, original.standard_code) == (name, code)


def test_missing_percentage_unit_preserves_review_and_calculation_gates(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-missing-percentage-unit')
    patient.sex = 'F'
    patient.save()
    indicator(patient, name='HCT', code='LAB_HCT', value='40', unit='')
    cell = comparable_cell(effective_rows(patient)[0])
    assert (cell.display_value, cell.unit) == ('40', '')
    assert cell.abnormal.symbol == ''
    assert not cell.plot_eligible and not cell.trend_eligible
    assert cell.review_required






def test_export_standard_reference_retains_its_unit_when_result_unit_is_unknown(django_user_model):
    from apps.exports.content import build_snapshot
    _, patient = _patient(django_user_model, 'catalog-export-uncertain-unit')
    patient.sex = 'F'
    patient.save()
    indicator(patient, unit='unknown')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    lab, = snapshot['labs']
    result, = snapshot['lab_results']
    for item in (lab, result):
        assert item['unit'] == 'unknown'
        assert item['standard_reference'] == '7–40'
        assert item['standard_reference_unit'] == 'U/L'


@pytest.mark.parametrize('name,panel,inside,confidence,expected', [
    ('颜色', '尿常规', True, '.99', '尿液颜色'), ('颜色', '大便常规', True, '.99', '粪便颜色'),
    ('颜色', '尿常规', False, '.99', None), ('颜色', '尿常规', True, '.8', None),
    ('8★颜色', '尿常规', True, '.99', '尿液颜色'),
])
def test_historical_color_uses_only_its_report_panel(django_user_model, name, panel, inside, confidence, expected):
    from dataclasses import replace
    from apps.labs.reports import persist_report_units
    from apps.processing.models import OcrBlock
    from tests.labs.test_report_identity import unit
    _, patient = _patient(django_user_model, 'catalog-historical-color')
    row = indicator(patient, name=name, code='CANDIDATE_COLOR', value='黄色', unit='', result_type='QUALITATIVE')
    row.specimen = ''
    row.save()
    report = replace(unit('采样时间：2026-09-22 08:30'), source_region=((0, 0), (1, 0), (1, .5), (0, .5)))
    persist_report_units(row.parsing_version, (report,))
    top = .2 if inside else .7
    OcrBlock.objects.create(parsing_version=row.parsing_version, document_page=row.document_page,
        reading_order=20, text=panel, confidence=confidence,
        polygon=[[.1, top], [.9, top], [.9, top + .05], [.1, top + .05]])
    cell = comparable_cell(effective_rows(patient)[0])
    assert (cell.catalog.indicator.name if cell.catalog else None) == expected
    assert cell.observation.specimen == '' and not cell.trend_eligible


def test_document_result_summary_uses_standard_values_and_keeps_raw_details(django_user_model):
    import re
    client, patient = _patient(django_user_model, 'catalog-document-summary')
    patient.sex = 'F'
    patient.save()
    row = indicator(patient, name='血红蛋白', code='LAB_HGB', value='16', unit='g/dL', reference='10-99')
    html = client.get(f'/records/{row.parsing_version.document_id}/').content.decode()
    summary = re.search(r'<summary class="observation-row">.*?</summary>', html, re.S).group()
    assert '<strong>160</strong>' in summary and 'g/L' in summary
    assert '标准参考范围：115–150 g/L' in summary
    assert '10-99' not in summary and '报告标记' not in summary
    assert '原报告：血红蛋白 · 16 g/dL · 参考：10-99' in html


@pytest.mark.parametrize('name,code,unit,panels,category,reference', [
    ('肌酐', 'LAB_CREA', 'μmol/L', ('肾功', '急肾功+肝功（急）'), '肾功', '41–73'),
    ('PGI', 'LAB_CATALOG_061', 'ng/mL', ('肿瘤标记物', '血清胃功能检测'), '血清胃功能检测', '70–160'),
    ('PGII', 'LAB_CATALOG_062', 'ng/mL', ('肿瘤标记物', '血清胃功能检测'), '血清胃功能检测', '5–60'),
    ('CRP', 'LAB_CRP', 'mg/L', ('血常规（急诊）', '炎症三项'), '炎症三项', '0–6'),
])
def test_historical_panels_use_unique_catalog_definition_in_history_and_output(
        django_user_model, name, code, unit, panels, category, reference):
    from apps.exports.content import build_snapshot
    _, patient = _patient(django_user_model, 'catalog-group-history-' + code)
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    originals = []
    for day, panel in enumerate(panels, 20):
        row = indicator(patient, name=name, code=code, unit=unit, value='80', day=date(2026, 9, day))
        row.field_evidence = {**row.field_evidence, 'panel': {'value': panel}}
        row.save()
        originals.append(row)
    table = comparison_view(patient)
    assert len(table.rows) == 1
    history = table.rows[0]
    cells = [cell for column in history.cells for cell in column]
    assert len(cells) == 2
    assert {cell.catalog.indicator.category for cell in cells} == {category}
    assert {cell.standard_reference for cell in cells} == {reference}
    assert history.category == category
    filtered = comparison_view(patient, category=category)
    assert len(filtered.rows) == 1 and filtered.result_count == 2
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert {row['standard_reference'] for row in snapshot['labs']} == {reference}
    assert len(snapshot['labs']) == 2
    for row, panel in zip(originals, panels):
        row.refresh_from_db()
        assert row.standard_code == code and row.field_evidence['panel']['value'] == panel


@pytest.mark.parametrize('name,code,unit,category,reference', [
    ('CRP', 'LAB_CRP', 'mg/L', '炎症三项', '0–6'),
    ('肌酐', 'LAB_CREA', 'μmol/L', '肾功', '41–73'),
    ('PGI', 'LAB_CATALOG_061', 'ng/mL', '血清胃功能检测', '70–160'),
    ('PGII', 'LAB_CATALOG_062', 'ng/mL', '血清胃功能检测', '5–60'),
])
def test_unique_catalog_definition_does_not_require_panel(
        django_user_model, name, code, unit, category, reference):
    _, patient = _patient(django_user_model, 'catalog-no-panel-' + code)
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    indicator(patient, name=name, code=code, unit=unit, value='8')
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.catalog.indicator.category == category
    assert cell.standard_reference == reference


@pytest.mark.parametrize('panel,confidence,inside', [
    ('急肾功+肝功（急）', '.99', True),
    ('肾功', '.99', True),
    ('急肾功+肝功（急）', '.8', True),
    ('急肾功+肝功（急）', '.99', False),
])
def test_historical_report_title_does_not_override_unique_creatinine_definition(
        django_user_model, panel, confidence, inside):
    from dataclasses import replace
    from apps.labs.reports import persist_report_units
    from apps.processing.models import OcrBlock
    from tests.labs.test_report_identity import unit
    _, patient = _patient(django_user_model, 'catalog-historical-duplicate')
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    row = indicator(patient, name='肌酐', code='LAB_CREA', value='80', unit='μmol/L')
    report = replace(unit('采样时间：2026-09-22 08:30'), source_region=((0, 0), (1, 0), (1, .5), (0, .5)))
    persist_report_units(row.parsing_version, (report,))
    top = .2 if inside else .7
    OcrBlock.objects.create(parsing_version=row.parsing_version, document_page=row.document_page,
        reading_order=20, text=panel, confidence=confidence,
        polygon=[[.1, top], [.9, top], [.9, top + .05], [.1, top + .05]])
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.catalog.indicator.category == '肾功'
    assert cell.standard_reference == '41–73'



def test_same_day_results_keep_sources_with_one_catalog_reference(django_user_model):
    from apps.labs.consolidation import fold_cells
    client, patient = _patient(django_user_model, 'catalog-group-fold')
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    for panel in ('肾功', '急肾功+肝功（急）'):
        row = indicator(patient, name='肌酐', code='LAB_CREA', unit='μmol/L', value='80')
        row.field_evidence = {**row.field_evidence, 'panel': {'value': panel}}
        row.save()
    cells = [comparable_cell(row) for row in effective_rows(patient)]
    assert {cell.standard_reference for cell in cells} == {'41–73'}
    folded = fold_cells(cells)
    assert len(folded) == 1 and len(folded[0].sources) == 2
    assert {cell.standard_reference for cell in folded} == {'41–73'}
    assert {source.pk for cell in folded for source in cell.sources} == {cell.observation.pk for cell in cells}
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    heading = re.search(r'<th scope="row">.*?</th>', html, re.S).group()
    references = re.findall(r'<small class="comparison-reference">.*?</small>', heading, re.S)
    assert len(references) == 1
    assert '41–73' in references[0]
    assert 'comparison-reference-date' not in heading
    assert all('标准参考范围：' not in result for result in re.findall(r'<article>.*?</article>', html, re.S))
