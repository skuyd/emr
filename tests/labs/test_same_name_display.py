from datetime import date

import pytest

from apps.labs.comparison import comparison_view
from apps.labs.dictionary import phase_two_dictionary
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_same_name_keeps_each_specimen_and_review_status_in_one_row(django_user_model):
    client, patient = _patient(django_user_model, 'same-name-specimens')
    originals = []
    for index, specimen in enumerate(('BLOOD', '', 'URINE'), 1):
        row = _observation(patient, date(2026, 8, index), str(index), code='LAB_HGB',
                           raw_name='血红蛋白', standard_name='血红蛋白', raw_unit='g/L')[1]
        row.dictionary_version = phase_two_dictionary().version
        row.specimen = specimen
        row.save()
        originals.append(row)
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    view = response.context['comparison']
    assert len(view.rows) == 1
    cells = [cell for column in view.rows[0].cells for cell in column]
    assert {cell.observation.pk for cell in cells} == {row.pk for row in originals}
    assert [cell.observation.specimen for cell in cells] == ['BLOOD', '', 'URINE']
    assert not cells[1].trend_eligible and not cells[2].trend_eligible
    html = response.content.decode()
    for label in ('标本：血液', '标本待确认', '标本：尿液', '指标待核对'):
        assert label in html


def test_unmapped_same_name_groups_without_folding_or_promoting_results(django_user_model):
    client, patient = _patient(django_user_model, 'same-name-candidates')
    originals = []
    for code in ('CANDIDATE_ONE', 'CANDIDATE_TWO'):
        row = _observation(patient, date(2026, 8, 1), '9', code=code, raw_name='合成未知指标')[1]
        originals.append(row)
    response = client.get('/labs/compare/', {'patient': patient.pk})
    view = response.context['comparison']
    assert len(view.rows) == 1
    assert view.result_count == 2
    cells = view.rows[0].cells[0]
    assert {source.pk for cell in cells for source in cell.sources} == {row.pk for row in originals}
    assert all(not cell.trend_eligible and not cell.plot_eligible for cell in cells)
    assert response.content.decode().count('指标待核对') == 2


def test_confirmed_alias_and_candidate_name_share_display_but_not_identity(django_user_model):
    _, patient = _patient(django_user_model, 'same-name-mapping')
    originals = []
    for day, code, name in ((1, 'CANDIDATE_TBA', '总胆汁酸'), (2, 'LAB_TBA', '14 ★TBA 总胆汁酸')):
        row = _observation(patient, date(2026, 8, day), '5', code=code, raw_name=name,
                           standard_name='总胆汁酸', raw_unit='umol/L')[1]
        row.dictionary_version = phase_two_dictionary().version
        row.specimen = 'BLOOD' if day == 1 else ''
        row.save()
        originals.append(row)
    view = comparison_view(patient, project='TBA')
    assert len(view.rows) == 1
    assert len(view.columns) == 2
    assert view.rows[0].standard_name == '总胆汁酸'
    assert {source.pk for column in view.rows[0].cells for cell in column for source in cell.sources} == {row.pk for row in originals}
    for row, code in zip(originals, ('CANDIDATE_TBA', 'LAB_TBA')):
        row.refresh_from_db()
        assert row.standard_code == code


def test_count_percentage_and_conflicting_names_remain_distinct(django_user_model):
    _, patient = _patient(django_user_model, 'same-name-boundaries')
    for code, name, unit in (('LAB_LYMPH_COUNT', '淋巴细胞计数', '10^9/L'),
                             ('LAB_LYMPH_PERCENT', '13★LYMPH%淋巴细胞百分比', '%'),
                             ('LAB_ALB', '白蛋白', 'g/L'), ('LAB_ALB', '前白蛋白', 'g/L')):
        row = _observation(patient, date(2026, 8, 1), '5', code=code, raw_name=name, raw_unit=unit)[1]
        row.dictionary_version = phase_two_dictionary().version
        row.save()
    view = comparison_view(patient)
    assert len(view.rows) == 4
    assert len({row.standard_name for row in view.rows}) == 4
    assert view.result_count == 4


def test_category_filter_keeps_same_name_history_across_conflicting_codes(django_user_model):
    _, patient = _patient(django_user_model, 'same-name-categories')
    originals = []
    for day, code in enumerate(('LAB_HGB', 'LAB_TBA'), 1):
        row = _observation(patient, date(2026, 8, day), '5', code=code,
                           raw_name='血红蛋白', standard_name='血红蛋白', raw_unit='g/L')[1]
        row.dictionary_version = phase_two_dictionary().version
        row.save()
        originals.append(row)
    for category in ('血常规（急诊）',):
        view = comparison_view(patient, category=category)
        assert len(view.rows) == 1 and view.result_count == 2
        assert {source.pk for column in view.rows[0].cells for cell in column for source in cell.sources} == {row.pk for row in originals}
        assert {code for code, label in view.categories} == {'血常规（急诊）'}


def test_decorated_conflicting_alias_keeps_review_and_blocks_wrong_trend(django_user_model):
    client, patient = _patient(django_user_model, 'same-name-conflict')
    row = _observation(patient, date(2026, 8, 1), '5', code='LAB_HGB',
                       raw_name='14 ★ALB 白蛋白', standard_name='血红蛋白', raw_unit='g/L')[1]
    row.dictionary_version = phase_two_dictionary().version
    row.save()
    response = client.get('/labs/compare/', {'patient': patient.pk})
    view = response.context['comparison']
    assert view.rows[0].standard_name == '白蛋白-ALB'
    cell = view.rows[0].cells[0][0]
    assert cell.identity_review_required
    assert not cell.plot_eligible and not cell.trend_eligible
    assert not view.rows[0].trend_links
    assert '指标待核对' in response.content.decode()
    row.refresh_from_db()
    assert row.standard_code == 'LAB_HGB' and row.raw_name == '14 ★ALB 白蛋白'


def test_candidate_can_be_searched_by_its_displayed_approved_name(django_user_model):
    _, patient = _patient(django_user_model, 'same-name-search-heading')
    row = _observation(patient, date(2026, 8, 1), '120', code='CANDIDATE_HGB',
                       raw_name='14 ★HGB', standard_name='HGB', raw_unit='g/L')[1]
    row.dictionary_version = phase_two_dictionary().version
    row.save()
    assert comparison_view(patient).rows[0].standard_name == '血红蛋白'
    view = comparison_view(patient, project='血红蛋白')
    assert len(view.rows) == 1 and view.result_count == 1
    assert view.rows[0].cells[0][0].observation.pk == row.pk
    assert not view.rows[0].cells[0][0].plot_eligible
