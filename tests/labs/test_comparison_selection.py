from datetime import date
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.labs.revisions import revise_observation
from tests.cancer_ordering.test_services import _collect, _select
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_catalog_projection import indicator
from tests.labs.test_report_relations import report
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def options(response):
    return response.context['comparison'].selection_groups


def selected_key(response, name):
    return next(item['key'] for group in options(response) for item in group['indicators'] if item['label'] == name)


def test_page_uses_catalog_order_and_only_current_patient_history(django_user_model):
    client, patient = _patient(django_user_model, 'selection-order')
    for name, code, unit in [('ALT', 'LAB_ALT', 'U/L'), ('CEA', 'LAB_CEA', 'ng/mL'),
                             ('血红蛋白', 'LAB_HGB', 'g/L'), ('白细胞', 'LAB_WBC', '10^9/L')]:
        indicator(patient, name=name, code=code, unit=unit)
    _observation(patient, date(2026, 8, 1), '9', code='CANDIDATE_OTHER', raw_name='合成表外指标')
    _, other = _patient(django_user_model, 'selection-other')
    indicator(other, name='TSH', code='LAB_TSH', unit='mIU/L')
    _collect(patient)
    _select(patient, 'MANUAL_PROFILE', profile='LUNG')

    response = client.get('/labs/compare/', {'patient': patient.pk})
    view = response.context['comparison']
    assert [group.category for group in view.groups] == ['血常规（急诊）', '肿瘤标记物', '肝功-肝细胞损伤', 'OTHER']
    assert [row.standard_code for row in view.rows] == ['LAB_WBC', 'LAB_HGB', 'LAB_CEA', 'LAB_ALT', 'CANDIDATE_OTHER']
    assert [group['category'] for group in options(response)] == ['血常规（急诊）', '肿瘤标记物', '肝功-肝细胞损伤', 'OTHER']
    assert [item['label'] for item in options(response)[0]['indicators']] == ['白细胞计数', '血红蛋白']
    assert all(item['selected'] for group in options(response) for item in group['indicators'])
    assert '甲状腺功能' not in response.content.decode()
    assert 'data-display-order' not in response.content.decode()


@pytest.mark.parametrize('submitted', [[], ['not-a-patient-indicator']])
def test_explicit_empty_or_unknown_selection_never_restores_all(django_user_model, submitted):
    client, patient = _patient(django_user_model, 'selection-empty')
    _observation(patient, date(2026, 8, 1), '5')
    response = client.get('/labs/compare/', {'patient': patient.pk, 'selection': '1', 'indicator': submitted})
    assert not response.context['comparison'].rows
    assert not response.context['comparison'].groups
    assert '未选择检验指标' in response.content.decode()
    assert all(not item['selected'] for group in options(response) for item in group['indicators'])


def test_selection_filters_intersect_dates_and_alias_search_without_losing_options(django_user_model):
    client, patient = _patient(django_user_model, 'selection-filter')
    _observation(patient, date(2026, 8, 1), '4', raw_name='WBC')
    current = _observation(patient, date(2026, 8, 2), '5', raw_name='白细胞')[1]
    indicator(patient, name='ALT', code='LAB_ALT', unit='U/L')
    initial = client.get('/labs/compare/', {'patient': patient.pk})
    key = selected_key(initial, '白细胞计数')
    response = client.get('/labs/compare/', {'patient': patient.pk, 'selection': '1', 'indicator': [key],
        'start': '2026-08-02', 'end': '2026-08-02', 'project': 'WBC'})
    view = response.context['comparison']
    assert len(view.rows) == 1 and view.result_count == 1
    assert view.rows[0].cells[0][0].observation.pk == current.pk
    assert len(options(response)) == 2
    assert selected_key(response, '白细胞计数') == key
    href = unescape(re.search(r'class="comparison-value [^"]+"[^>]*href="([^"]+)"', response.content.decode()).group(1))
    return_to = parse_qs(urlsplit(href).query)['return_to'][0]
    assert parse_qs(urlsplit(return_to).query) == {'patient': [str(patient.pk)], 'selection': ['1'],
        'indicator': [key], 'start': ['2026-08-02'], 'end': ['2026-08-02'], 'project': ['WBC']}
    detail = client.get(href)
    assert detail.status_code == 200 and return_to in detail.context['comparison_return']


def test_cross_category_aliases_share_selection_and_one_history(django_user_model):
    client, patient = _patient(django_user_model, 'selection-cross-category')
    for day, name, panel in [(20, 'CRP', '血常规（急诊）'), (21, 'C反应蛋白', '炎症三项')]:
        row = indicator(patient, name=name, code='LAB_CRP', unit='mg/L', day=date(2026, 9, day))
        row.field_evidence = {**row.field_evidence, 'panel': {'value': panel}}
        row.save()
    initial = client.get('/labs/compare/', {'patient': patient.pk})
    groups = options(initial)
    assert [group['category'] for group in groups] == ['血常规（急诊）', '炎症三项']
    keys = [item['key'] for group in groups for item in group['indicators']]
    assert len(keys) == 2 and keys[0] == keys[1]
    response = client.get('/labs/compare/', {'patient': patient.pk, 'selection': '1', 'indicator': keys})
    assert len(response.context['comparison'].rows) == 1
    assert response.context['comparison'].result_count == 2


def test_same_raw_name_different_catalog_objects_have_separate_selection(django_user_model):
    client, patient = _patient(django_user_model, 'selection-colors')
    for specimen, panel in [('URINE', '尿常规'), ('STOOL', '大便常规+隐血')]:
        row = indicator(patient, name='颜色', code='CANDIDATE_COLOR', value='黄色', unit='')
        row.specimen, row.field_evidence = specimen, {'panel': {'value': panel}}
        row.save()
    initial = client.get('/labs/compare/', {'patient': patient.pk})
    urine, stool = selected_key(initial, '尿液颜色'), selected_key(initial, '粪便颜色')
    assert urine != stool
    response = client.get('/labs/compare/', {'patient': patient.pk, 'selection': '1', 'indicator': [urine]})
    assert [row.standard_name for row in response.context['comparison'].rows] == ['尿液颜色']


@pytest.mark.parametrize('state,prompt', [('AUTOMATIC', '待核对'), ('CONFIRM', ''),
    ('REPORT_ERROR', '识别有误'), ('revision_conflict', '修订冲突')])
def test_single_source_hides_entry_and_preserves_value_and_state(django_user_model, state, prompt):
    client, patient = _patient(django_user_model, 'selection-single-' + state)
    row = _observation(patient, date(2026, 8, 1), '5')[1]
    if state in {'CONFIRM', 'REPORT_ERROR'}:
        revise_observation(patient.account, row.pk, action=state, changes={}, expected_revision=0)
    elif state == 'revision_conflict':
        row.quality_issues = [{'code': 'revision_conflict', 'fields': ['raw_value']}]
        row.save(update_fields=['quality_issues'])
    response = client.get('/labs/compare/', {'patient': patient.pk})
    article = re.search(r'<article>.*?</article>', response.content.decode(), re.S).group()
    assert '1 个来源' not in article and 'comparison-sources' not in article
    assert f'/labs/observations/{row.pk}/' in article
    if prompt:
        assert prompt in article


def test_report_conflict_stays_visible_without_single_source_entry(django_user_model):
    client, patient = _patient(django_user_model, 'selection-report-conflict')
    report(patient, value='5')
    report(patient, value='6')
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    assert '1 个来源' not in html
    assert '报告归属存在冲突' in html
    assert html.count('class="comparison-value ') == 2


def test_multiple_sources_keep_each_original_and_review_link(django_user_model):
    client, patient = _patient(django_user_model, 'selection-many-sources')
    first = report(patient)[1]
    second = report(patient)[1]
    html = client.get('/labs/compare/', {'patient': patient.pk}).content.decode()
    assert html.count('2 个来源') == 1
    assert 'comparison-sources' in html
    for row in (first, second):
        assert f'/labs/observations/{row.pk}/source/raw_value/' in html
        assert f'/labs/observations/{row.pk}/' in html
    assert '采样：2026-09-17 08:30' in html and '报告号：A100' in html


def test_single_source_report_reparse_conflict_keeps_explicit_reason(django_user_model):
    from dataclasses import replace
    from apps.labs.reports import _from_snapshot, correct_report
    from tests.labs.test_report_revision_versions import SOURCE, next_report_version

    client, patient = _patient(django_user_model, 'selection-report-reparse-conflict')
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '核对后的医院'},
        expected_revision=0, source_evidence=SOURCE, rationale='原件医院', operation_id='hospital')
    next_report_version(original, (replace(_from_snapshot(original.automatic), report_number='CHANGED'),))
    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert len(cell.sources) == 1 and not cell.observation.report_conflict
    assert cell.observation.report_identity.reason == 'report_revision_conflict'
    assert 'report_identity_conflict' in {item['code'] for item in cell.quality_issues}
    article = re.search(r'<article>.*?</article>', response.content.decode(), re.S).group()
    assert '1 个来源' not in article and 'comparison-sources' not in article
    assert f'/labs/observations/{cell.observation.pk}/' in article
    assert '本次识别依据与既有人工修订不一致，请核对新旧证据' in article


def test_single_source_report_identity_conflict_keeps_explicit_reason(django_user_model):
    from apps.labs.reports import persist_report_units
    from tests.labs.test_report_identity import unit

    client, patient = _patient(django_user_model, 'selection-single-identity-conflict')
    row = _observation(patient, date(2026, 9, 17), '5')[1]
    identity = unit('采样时间：2026-09-17 08:30', '姓名：合成人甲', '姓名：合成人乙')
    assert identity.reason == 'report_identity_conflict'
    persist_report_units(row.parsing_version, (identity,))
    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert len(cell.sources) == 1 and not cell.observation.report_conflict
    assert cell.observation.report_identity.reason == 'report_identity_conflict'
    article = re.search(r'<article>.*?</article>', response.content.decode(), re.S).group()
    assert '1 个来源' not in article and 'comparison-sources' not in article
    assert f'/labs/observations/{row.pk}/' in article
    assert '报告身份存在冲突，请核对原件' in article


def test_single_source_retained_report_conflict_issue_stays_visible(django_user_model):
    client, patient = _patient(django_user_model, 'selection-retained-report-conflict')
    row = _observation(patient, date(2026, 8, 1), '5')[1]
    row.quality_issues = [{'code': 'report_identity_conflict', 'fields': []}]
    row.save(update_fields=['quality_issues'])
    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert len(cell.sources) == 1 and not cell.observation.report_conflict
    assert cell.observation.report_identity.status == 'ACCEPTED'
    assert 'report_identity_conflict' in {item['code'] for item in cell.quality_issues}
    article = re.search(r'<article>.*?</article>', response.content.decode(), re.S).group()
    assert '1 个来源' not in article and 'comparison-sources' not in article
    assert '报告归属存在冲突' in article
