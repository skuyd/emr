from datetime import date
from decimal import Decimal

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
    assert alt.category == '肝功-肝细胞损伤'
    assert sum(len(cells) for cells in alt.cells) == 3
    assert any(row.standard_name == '表外项目' and row.category == 'OTHER' for row in comparison.rows)


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
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}).content.decode()
    assert '标准参考范围' in detail and '115–150' in detail
    assert '16 g/dL' in detail and '0-99' in detail


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
    from apps.labs.trends import trend_view
    client, patient = _patient(django_user_model, 'catalog-legacy-name')
    indicator(patient, name='ALT', day=date(2026, 9, 20))
    indicator(patient, name='表外项目', code='LAB_ALT', day=date(2026, 9, 21))
    table = comparison_view(patient)
    assert len(table.rows) == 2
    assert {(row.standard_name, row.category) for row in table.rows} == {
        ('丙氨酸氨基转移酶', '肝功-肝细胞损伤'), ('表外项目', 'OTHER')}
    trend = trend_view(patient, 'LAB_ALT', include_history=True)
    assert [cell.observation.raw_name for cell in trend.daily_details] == ['ALT']
    other = client.get('/trends/LAB_ALT/', {'patient': patient.pk, 'history': '1', 'raw_name': '表外项目'})
    assert other.status_code == 200
    assert other.context['trend'].standard_name == '表外项目'
    assert [cell.observation.raw_name for cell in other.context['trend'].daily_details] == ['表外项目']


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


def test_document_trend_link_keeps_uncataloged_result_identity(django_user_model):
    from urllib.parse import parse_qs, urlsplit
    import re
    from tests.labs.test_trends import _observation
    client, patient = _patient(django_user_model, 'catalog-other-document-trend')
    for day in (20, 21):
        _observation(patient, date(2026, 9, day), '5')
    _, other = _observation(patient, date(2026, 9, 22), '6', raw_name='Uncataloged')
    html = client.get(f'/records/{other.parsing_version.document_id}/').content.decode()
    link = re.search(r'class="observation-trend" href="([^"]+)"', html).group(1).replace('&amp;', '&')
    assert parse_qs(urlsplit(link).query)['raw_name'] == ['Uncataloged']
    response = client.get(link)
    assert response.status_code == 200
    assert response.context['trend'].standard_name == 'Uncataloged'
    assert [cell.observation.raw_name for cell in response.context['trend'].daily_details] == ['Uncataloged']


def test_trend_history_groups_catalog_aliases_without_granting_calculation(django_user_model):
    from apps.labs.trends import trend_view
    _, patient = _patient(django_user_model, 'catalog-trend-alias')
    indicator(patient, name='谷丙转氨酶', code='CANDIDATE_ALT_A', day=date(2026, 9, 20))
    indicator(patient, name='ALT', code='CANDIDATE_ALT_B', day=date(2026, 9, 21))
    view = trend_view(patient, 'LAB_ALT', include_history=True)
    assert view is not None and view.standard_name == '丙氨酸氨基转移酶'
    assert sum(len(series.points) for series in view.series) == 2
    assert all(series.historical and not series.segments for series in view.series)
    assert trend_view(patient, 'LAB_ALT') is None


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


@pytest.mark.parametrize('panel,inside,confidence,expected', [
    ('尿常规', True, '.99', '尿液颜色'), ('大便常规', True, '.99', '粪便颜色'),
    ('尿常规', False, '.99', None), ('尿常规', True, '.8', None),
])
def test_historical_color_uses_only_its_report_panel(django_user_model, panel, inside, confidence, expected):
    from dataclasses import replace
    from apps.labs.reports import persist_report_units
    from apps.processing.models import OcrBlock
    from tests.labs.test_report_identity import unit
    _, patient = _patient(django_user_model, 'catalog-historical-color')
    row = indicator(patient, name='颜色', code='CANDIDATE_COLOR', value='黄色', unit='', result_type='QUALITATIVE')
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


@pytest.mark.parametrize('name,code,unit,panels,ranges', [
    ('肌酐', 'LAB_CREA', 'μmol/L', ('肾功', '急肾功+肝功（急）'), ('41–73', '44–133')),
    ('PGI', 'LAB_CATALOG_061', 'ng/mL', ('肿瘤标记物', '血清胃功能检测'), ('70–160', '70–165')),
    ('PGII', 'LAB_CATALOG_062', 'ng/mL', ('肿瘤标记物', '血清胃功能检测'), ('5–60', '3–15')),
    ('CRP', 'LAB_CRP', 'mg/L', ('血常规（急诊）', '炎症三项'), ('0–6', '0–6')),
])
def test_report_group_ranges_stay_with_each_result_in_history_and_output(django_user_model, name, code, unit, panels, ranges):
    from apps.exports.content import build_snapshot
    from apps.labs.trends import trend_view
    _, patient = _patient(django_user_model, 'catalog-group-history-' + code)
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    for day, panel in enumerate(panels, 20):
        row = indicator(patient, name=name, code=code, unit=unit, value='80', day=date(2026, 9, day))
        row.field_evidence = {**row.field_evidence, 'panel': {'value': panel}}
        row.save()
    table = comparison_view(patient)
    assert len(table.rows) == 1
    history = table.rows[0]
    cells = [cell for column in history.cells for cell in column]
    assert {cell.catalog.indicator.category: cell.standard_reference for cell in cells} == dict(zip(panels, ranges))
    assert all(panel in history.category for panel in panels)
    for panel in panels:
        assert len(comparison_view(patient, category=panel).rows) == 1
    trend = trend_view(patient, code, include_history=True)
    assert {point.catalog.reference.label for point in trend.daily_details} == set(ranges)
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert {row['standard_reference'] for row in snapshot['labs']} == set(ranges)


def test_duplicate_reference_does_not_guess_report_category_from_dictionary(django_user_model):
    _, patient = _patient(django_user_model, 'catalog-no-panel')
    indicator(patient, name='CRP', code='LAB_CRP', unit='mg/L', value='8')
    cell = comparable_cell(effective_rows(patient)[0])
    assert cell.catalog is None
    assert cell.standard_reference == ''


@pytest.mark.parametrize('panel,confidence,inside,expected', [
    ('急肾功+肝功（急）', '.99', True, '44–133'),
    ('肾功', '.99', True, '41–73'),
    ('急肾功+肝功（急）', '.8', True, None),
    ('急肾功+肝功（急）', '.99', False, None),
])
def test_historical_duplicate_uses_only_confident_title_inside_report(django_user_model, panel, confidence, inside, expected):
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
    assert (cell.catalog.reference.label if cell.catalog else None) == expected



def test_same_day_equal_results_keep_distinct_report_group_references(django_user_model):
    from apps.labs.consolidation import fold_cells
    _, patient = _patient(django_user_model, 'catalog-group-fold')
    patient.sex, patient.birth_date = 'F', date(2000, 1, 1)
    patient.save()
    for panel in ('肾功', '急肾功+肝功（急）'):
        row = indicator(patient, name='肌酐', code='LAB_CREA', unit='μmol/L', value='80')
        row.field_evidence = {**row.field_evidence, 'panel': {'value': panel}}
        row.save()
    cells = [comparable_cell(row) for row in effective_rows(patient)]
    assert {cell.standard_reference for cell in cells} == {'41–73', '44–133'}
    folded = fold_cells(cells)
    assert len(folded) == 2
    assert {cell.standard_reference for cell in folded} == {'41–73', '44–133'}
