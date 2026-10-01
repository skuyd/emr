from datetime import date

import pytest

from apps.labs.comparison import comparable_cell, comparison_view
from apps.labs.revisions import effective_observation, revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def confirm(patient, row):
    revise_observation(patient.account, row.pk, action='CONFIRM', changes={}, expected_revision=0)
    row.refresh_from_db()


def test_confirmation_removes_identity_and_result_warnings_from_comparison(django_user_model):
    client, patient = _patient(django_user_model, 'confirmed-display')
    _, row = _observation(patient, date(2026, 8, 1), '12')
    row.evidence.confidence = '.4'
    row.evidence.save(update_fields=['confidence'])
    confirm(patient, row)

    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert cell.result_confirmed
    assert not cell.identity_review_required and not cell.review_required
    html = response.content.decode()
    assert '指标待核对' not in html and '结果待核对' not in html
    assert 'comparison-value--review' not in html
    assert 'comparison-abnormal--above' in html


@pytest.mark.parametrize('unit', ['10⁹/L', '×10^9/L', 'X10^9/L', 'x10^9/L', 'x10⁹/L', '109/L', '×109/L', 'X109/L'])
def test_equivalent_count_units_share_table_unit_without_calculation_warning(django_user_model, unit):
    client, patient = _patient(django_user_model, 'confirmed-equivalent-count-unit')
    _, first = _observation(patient, date(2026, 8, 1), '4.20')
    _, second = _observation(patient, date(2026, 8, 2), '5.20', raw_unit=unit)
    confirm(patient, first)
    confirm(patient, second)

    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    result, = response.context['comparison'].rows
    assert result.shared_unit == '10^9/L'
    cells = [cell for column in result.cells for cell in column]
    assert [cell.display_value for cell in cells] == ['4.20', '5.20']
    assert all(cell.trend_eligible and cell.plot_eligible for cell in cells)
    assert all('unit_unknown' not in {issue['code'] for issue in cell.quality_issues} for cell in cells)
    assert all(not cell.calculation_limit_labels for cell in cells)
    assert '单位无法换算' not in response.content.decode()
    second.refresh_from_db()
    assert (second.raw_value, second.raw_unit) == ('5.20', unit)


@pytest.mark.parametrize('unit', ['10⁹/L', '×10^9/L', 'X10^9/L', 'x10^9/L', '109/L', '×109/L', 'X109/L'])
def test_count_unit_display_is_normalized_without_granting_unknown_indicator_calculations(django_user_model, unit):
    client, patient = _patient(django_user_model, 'unknown-indicator-count-unit')
    _, row = _observation(patient, date(2026, 8, 1), '5.20', code='CANDIDATE_UNMAPPED',
                          raw_name='目录外合成计数项目', standard_name='目录外合成计数项目', raw_unit=unit)
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.catalog is None
    assert (cell.display_value, cell.unit) == ('5.20', '10^9/L')
    assert not cell.trend_eligible and not cell.plot_eligible
    assert '缺少标准指标' in cell.calculation_limit_labels
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    assert detail.status_code == comparison.status_code == 200
    assert detail.context['row_items'][0]['cell'].unit == '10^9/L'
    row.refresh_from_db()
    assert (row.raw_value, row.raw_unit) == ('5.20', unit)


@pytest.mark.parametrize('unit,expected', [
    ('X1012/ML', '10^12/mL'), ('106/L', '10^6/L'), ('109', '10^9'),
    ('Umo1/L', 'μmol/L'), ('Mmo1/L', 'mmol/L'), ('ug/L', 'μg/L'),
])
def test_general_unit_spelling_is_displayed_without_mapping_unknown_indicators(django_user_model, unit, expected):
    client, patient = _patient(django_user_model, 'unknown-general-unit')
    _, row = _observation(patient, date(2026, 8, 1), '109', code='CANDIDATE_UNMAPPED',
                          raw_name='目录外合成项目', standard_name='目录外合成项目', raw_unit=unit)
    confirm(patient, row)
    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert (cell.display_value, cell.unit) == ('109', expected)
    assert not cell.known_unit and not cell.plot_eligible and not cell.trend_eligible
    assert '缺少标准指标' in cell.calculation_limit_labels
    row.refresh_from_db()
    assert (row.raw_value, row.raw_unit) == ('109', unit)


def test_confirmed_historical_percentage_recovers_only_missing_unit_capability(django_user_model):
    client, patient = _patient(django_user_model, 'historical-percent-capability')
    _, row = _observation(patient, date(2026, 8, 1), '40', code='LAB_HCT',
                          raw_name='HCT', standard_name='HCT', raw_unit='')
    row.capability_level = 'SEARCH_ONLY'
    row.quality_issues = [{'code': 'unit_unknown', 'fields': ['raw_unit']}]
    row.save(update_fields=['capability_level', 'quality_issues'])
    confirm(patient, row)
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert cell.unit == '%' and cell.plot_eligible and cell.trend_eligible
    row.refresh_from_db()
    assert row.raw_unit == '' and row.capability_level == 'SEARCH_ONLY'


@pytest.mark.parametrize('name,code,unit,canonical_unit', [
    ('肌酐', 'LAB_CREA', 'umo1/L', 'μmol/L'),
    ('尿素', 'LAB_UREA', 'mmo1/L', 'mmol/L'),
    ('尿素', 'LAB_UREA', 'Mmo1/L', 'mmol/L'),
    ('肌酐', 'LAB_CREA', 'Umo1/L', 'μmol/L'),
])
def test_molar_unit_ocr_digit_does_not_block_confirmed_result(django_user_model, name, code, unit, canonical_unit):
    client, patient = _patient(django_user_model, 'confirmed-molar-ocr-unit')
    _, row = _observation(patient, date(2026, 8, 1), '12.30', code=code,
                          raw_name=name, standard_name=name, raw_unit=unit)
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert (cell.display_value, cell.unit) == ('12.30', canonical_unit)
    assert cell.known_unit
    assert 'unit_unknown' not in {issue['code'] for issue in cell.quality_issues}
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        assert response.status_code == 200
        assert '单位无法换算' not in response.content.decode()
    row.refresh_from_db()
    assert (row.raw_value, row.raw_unit) == ('12.30', unit)


@pytest.mark.parametrize('name,code', [
    ('白球比值', 'LAB_A_G_RATIO'), ('尿比重', 'LAB_URINE_SPECIFIC_GRAVITY'),
])
@pytest.mark.parametrize('value,result_type', [('1.20', 'NUMERIC'), ('<1.20', 'COMPARATOR')])
def test_catalog_dimensionless_result_does_not_require_a_unit(django_user_model, name, code, value, result_type):
    client, patient = _patient(django_user_model, 'confirmed-dimensionless-unit')
    _, row = _observation(patient, date(2026, 8, 1), value, code=code,
                          raw_name=name, standard_name=name, raw_unit='', result_type=result_type)
    if code == 'LAB_URINE_SPECIFIC_GRAVITY':
        row.specimen = 'URINE'
        row.save(update_fields=['specimen'])
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.catalog and cell.catalog.indicator.unit == '' and cell.known_unit
    assert (cell.display_value, cell.unit) == (value, '')
    assert '缺少单位' not in cell.calculation_limit_labels
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        assert response.status_code == 200
        assert '缺少单位' not in response.content.decode()
        assert '单位无法换算' not in response.content.decode()
    row.refresh_from_db()
    assert (row.raw_value, row.raw_unit) == (value, '')


@pytest.mark.parametrize('name,code', [
    ('中性粒细胞百分比', 'LAB_NEUT_PERCENT'), ('HCT', 'LAB_HCT'),
])
@pytest.mark.parametrize('unit', ['%', '％', '', '  '])
def test_percentage_result_uses_catalog_unit_when_ocr_unit_is_blank(django_user_model, name, code, unit):
    client, patient = _patient(django_user_model, 'confirmed-percentage-unit')
    _, row = _observation(patient, date(2026, 8, 1), '40.0', code=code,
                          raw_name=name, standard_name=name, raw_unit=unit)
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.display_value == '40.0'
    assert '缺少单位' not in cell.calculation_limit_labels
    assert cell.unit == '%' and cell.known_unit
    assert 'unit_unknown' not in {issue['code'] for issue in cell.quality_issues}
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        assert response.status_code == 200
        assert '缺少单位' not in response.content.decode()
    row.refresh_from_db()
    assert (row.raw_value, row.raw_unit) == ('40.0', unit)


@pytest.mark.parametrize('unit', ['%', '％', '', '  '])
def test_unmapped_percentage_keeps_explicit_unit_without_filling_blank_unit(django_user_model, unit):
    _, patient = _patient(django_user_model, 'unmapped-percentage-unit')
    _, row = _observation(patient, date(2026, 8, 1), '23.5', code='CANDIDATE_LARGE_PLATELET_RATIO',
                          raw_name='大型血小板比率', standard_name='大型血小板比率', raw_unit=unit)
    confirm(patient, row)
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert cell.catalog is None and not cell.known_unit
    assert not cell.plot_eligible and not cell.trend_eligible
    assert cell.unit == ('%' if unit.strip() else '')
    assert ('缺少单位' in cell.calculation_limit_labels) == (not unit.strip())
    assert '缺少标准指标' in cell.calculation_limit_labels


@pytest.mark.parametrize('changes,label', [
    ({'specimen': ''}, '缺少标本'),
    ({'raw_unit': ''}, '缺少单位'),
    ({'raw_unit': 'unverified-unit'}, '单位无法换算'),
    ({'method_raw': ''}, '缺少检测方法'),
    ({'raw_name': '目录外合成项目', 'standard_code': 'CANDIDATE_UNMAPPED'}, '缺少标准指标'),
    ({'raw_value': '1e1000000'}, '结果无法计算'),
])
def test_confirmed_missing_calculation_basis_is_specific_in_both_views(django_user_model, changes, label):
    client, patient = _patient(django_user_model, 'confirmed-incomplete-display')
    _, row = _observation(patient, date(2026, 8, 1), '12')
    for field, value in changes.items():
        setattr(row, field, value)
    row.save(update_fields=list(changes))
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.result_confirmed and not cell.trend_eligible
    assert not cell.identity_review_required and not cell.review_required
    assert cell.abnormal.status != 'review'
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        assert response.status_code == 200
        html = response.content.decode()
        if label in {'缺少标本', '缺少检测方法'}:
            assert label not in html
        else:
            assert '计算信息不足：' in html and label in html
        assert '标本待确认' not in html and '指标待核对' not in html and '结果待核对' not in html
    if 'specimen' in changes:
        assert detail.context['row_items'][0]['cell'].specimen_label == '未提供'
    row.refresh_from_db()
    assert all(getattr(row, field) == value for field, value in changes.items())


@pytest.mark.parametrize('specimen,method', [('', ''), ('BLOOD', '合成方法A')])
@pytest.mark.parametrize('confirmed', [False, True])
def test_lab_pages_omit_specimen_and_method_without_changing_stored_values(
        django_user_model, specimen, method, confirmed):
    client, patient = _patient(django_user_model, 'lab-display-without-specimen-method')
    _, row = _observation(patient, date(2026, 8, 1), '12', method=method)
    row.specimen = specimen
    row.reference_range_raw = '3-9'
    row.quality_issues = [{'code': 'recognition_uncertain', 'fields': ['method_raw'],
                           'details': '检测方法需要核对'}]
    row.save(update_fields=['specimen', 'reference_range_raw', 'quality_issues'])
    if confirmed:
        confirm(patient, row)

    responses = [client.get('/labs/compare/', {'patient': patient.pk}),
                 client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)]
    row.parsing_version.active = False
    row.parsing_version.save(update_fields=['active'])
    responses.append(client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}))
    for response in responses:
        assert response.status_code == 200
        html = response.content.decode()
        assert '标本' not in html and '检测方法' not in html
        assert 'name="specimen"' not in html and 'name="method_raw"' not in html
        assert '计算信息不足：' not in html
    row.refresh_from_db()
    assert (row.specimen, row.method_raw) == (specimen, method)


def test_confirmed_result_does_not_hide_a_new_report_conflict(django_user_model):
    client, patient = _patient(django_user_model, 'confirmed-report-conflict')
    _, row, _ = report(patient)
    confirm(patient, row)
    report(patient, value='6')

    response = client.get('/labs/compare/', {'patient': patient.pk})
    cells = [cell for result in response.context['comparison'].rows for column in result.cells for cell in column]
    confirmed = next(cell for cell in cells if cell.observation.pk == row.pk)
    assert not confirmed.result_confirmed
    assert not confirmed.trend_eligible
    assert '报告归属存在冲突' in response.content.decode()


def test_folded_result_requires_every_source_to_be_confirmed(django_user_model):
    _, patient = _patient(django_user_model, 'confirmed-folded-sources')
    _, row, _ = report(patient)
    report(patient)
    confirm(patient, row)
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert len(cell.sources) == 2
    assert not cell.result_confirmed
    assert '待核对' in cell.source_review_labels


def test_confirmed_missing_date_keeps_a_specific_calculation_limit(django_user_model):
    _, patient = _patient(django_user_model, 'confirmed-date-display')
    _, row = _observation(patient, date(2026, 8, 1), '12')
    row.observation_date = None
    row.save(update_fields=['observation_date'])
    confirm(patient, row)
    cell = comparable_cell(effective_observation(row))
    assert not cell.trend_eligible
    assert '缺少日期' in cell.calculation_limit_labels


def test_confirmation_help_focuses_on_checking_report_content(django_user_model):
    client, patient = _patient(django_user_model, 'confirmed-help')
    _, row, _ = report(patient)
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    batch = client.get('/labs/reports/batch-confirmation/', {'patient': patient.pk}, follow=True)
    for response in (detail, batch):
        html = response.content.decode()
        assert '请逐页对照原件，再保存或确认本报告。' in html
        assert '标本' not in html and '检测方法' not in html


@pytest.mark.parametrize('code', ['association_conflict', 'recognition_uncertain'])
@pytest.mark.parametrize('field,label', [
    ('method_raw', '检测方法依据不足'),
    ('observation_date', '日期依据不足'),
])
def test_confirmed_result_explains_uncertain_method_or_date_basis(django_user_model, code, field, label):
    client, patient = _patient(django_user_model, 'confirmed-basis-display')
    _, row = _observation(patient, date(2026, 8, 1), '12')
    row.quality_issues = [{'code': code, 'fields': ['raw_name', field]}]
    row.save(update_fields=['quality_issues'])
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.result_confirmed and not cell.trend_eligible
    assert getattr(cell.observation, field)
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        html = response.content.decode()
        if field == 'method_raw':
            assert '计算信息不足：' not in html and label not in html
        else:
            assert '计算信息不足：' in html and label in html
        assert '标本待确认' not in html and '指标待核对' not in html and '结果待核对' not in html


@pytest.mark.parametrize('flag,label', [
    ('unrecognized', '报告原标记无法用于异常计算'),
    ('H', '报告原标记与参考范围不一致，无法计算异常状态'),
])
def test_confirmed_report_flag_calculation_limit_is_visible_in_both_views(django_user_model, flag, label):
    client, patient = _patient(django_user_model, 'confirmed-flag-display')
    _, row = _observation(patient, date(2026, 8, 1), '5', raw_name='目录外合成项目')
    row.report_flag_raw = flag
    row.reference_range_raw = '1-10'
    row.save(update_fields=['report_flag_raw', 'reference_range_raw'])
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.catalog is None and cell.result_confirmed
    assert cell.abnormal.status == 'unavailable'
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        html = response.content.decode()
        assert '计算信息不足：' in html and label in html
        assert '结果待核对' not in html
    row.refresh_from_db()
    assert row.report_flag_raw == flag


@pytest.mark.parametrize('value,result_type,code,reference,status,note', [
    ('<5', 'COMPARATOR', 'LAB_WBC', '5-10', 'below', '比较符结果不参与数值趋势或变化计算'),
    ('阴性', 'QUALITATIVE', 'LAB_HBSAG', '阴性', 'within', '非数值结果不参与数值趋势或变化计算'),
])
def test_confirmed_non_numeric_result_explains_trend_without_changing_reference_comparison(
        django_user_model, value, result_type, code, reference, status, note):
    client, patient = _patient(django_user_model, 'confirmed-nonnumeric-display')
    _, row = _observation(patient, date(2026, 8, 1), value, raw_name='目录外合成项目',
                          code=code, result_type=result_type, raw_unit='10^9/L' if result_type == 'COMPARATOR' else '')
    row.reference_range_raw = reference
    row.save(update_fields=['reference_range_raw'])
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.result_confirmed and cell.numeric_value is None
    assert not cell.trend_eligible and not cell.plot_eligible
    assert cell.abnormal.status == status
    assert not cell.calculation_limit_labels
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        html = response.content.decode()
        assert note in html
        assert '计算信息不足：' not in html and '结果待核对' not in html
    row.refresh_from_db()
    assert row.raw_value == value and row.result_type == result_type


@pytest.mark.parametrize('raw_name,needs_explanation', [('目录外合成项目', True), ('白细胞', False)])
def test_confirmed_unusable_report_reference_is_explained_unless_catalog_supplies_basis(
        django_user_model, raw_name, needs_explanation):
    client, patient = _patient(django_user_model, 'confirmed-reference-display')
    _, row = _observation(patient, date(2026, 8, 1), '5', raw_name=raw_name)
    row.reference_range_raw = '10-5'
    row.save(update_fields=['reference_range_raw'])
    confirm(patient, row)

    comparison = client.get('/labs/compare/', {'patient': patient.pk})
    cell = comparison.context['comparison'].rows[0].cells[0][0]
    assert cell.result_confirmed
    assert cell.abnormal.status == ('unavailable' if needs_explanation else 'within')
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    for response in (comparison, detail):
        html = response.content.decode()
        assert ('参考范围无法计算' in html) == needs_explanation
        assert '结果待核对' not in html
    row.refresh_from_db()
    assert row.reference_range_raw == '10-5'
