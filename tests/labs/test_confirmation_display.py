from datetime import date

import pytest

from apps.labs.comparison import comparable_cell, comparison_view
from apps.labs.revisions import effective_observation, revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report
from tests.labs.helpers import _observation


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
        assert '计算信息不足：' in html and label in html
        assert '标本待确认' not in html and '指标待核对' not in html and '结果待核对' not in html
    if 'specimen' in changes:
        assert detail.context['row_items'][0]['cell'].specimen_label == '未提供'
    row.refresh_from_db()
    assert all(getattr(row, field) == value for field, value in changes.items())


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


def test_confirmation_help_describes_verified_fields_and_remaining_calculation_limits(django_user_model):
    client, patient = _patient(django_user_model, 'confirmed-help')
    _, row, _ = report(patient)
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk}, follow=True)
    batch = client.get('/labs/reports/batch-confirmation/', {'patient': patient.pk}, follow=True)
    for response in (detail, batch):
        html = response.content.decode()
        assert '标本、指标和结果' in html
        assert '也不解除原有比较限制' not in html
        assert '计算信息不足' in html


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
