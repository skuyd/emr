from types import SimpleNamespace

import pytest

from apps.labs.trend_forms import JointTrendForm
from tests.cancer_ordering.test_lab_ordering import labs
from tests.cancer_ordering.test_services import _collect, _select
from tests.documents.test_detail_viewer import _patient


@pytest.mark.django_db
@pytest.mark.parametrize('selected,previous,expected', [
    (['LAB_CA19_9', 'LAB_CEA', 'LAB_WBC'], ['LAB_CEA', 'LAB_WBC', 'LAB_CA19_9'], ['LAB_CEA', 'LAB_WBC', 'LAB_CA19_9']),
    (['LAB_CA19_9', 'LAB_WBC'], ['LAB_CEA', 'LAB_WBC', 'LAB_CA19_9'], ['LAB_WBC', 'LAB_CA19_9']),
    (['LAB_CA19_9', 'LAB_CEA', 'LAB_WBC'], ['LAB_WBC', 'LAB_CA19_9'], ['LAB_WBC', 'LAB_CA19_9', 'LAB_CEA']),
    (['LAB_CA19_9', 'LAB_CEA', 'LAB_WBC'], [], ['LAB_CA19_9', 'LAB_CEA', 'LAB_WBC']),
    ([], ['LAB_CEA', 'LAB_WBC', 'LAB_CA19_9'], []),
])
def test_resubmission_keeps_remaining_explicit_order_then_appends_new_selections(django_user_model, selected, previous, expected):
    client, patient = _patient(django_user_model, 'joint-order-contract')
    labs(patient)
    _collect(patient)
    _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    response = client.get('/trends/compare/', {'code': selected, 'code_order': previous, 'start': '2026-06-01'})
    assert response.status_code == 200
    assert [graph.standard_code for graph in response.context['trends']] == expected
    assert list(response.context['form'].cleaned_data['code']) == expected
    html = response.content.decode()
    positions = [html.index(f'name="code_order" value="{code}"') for code in expected]
    assert positions == sorted(positions)


@pytest.mark.parametrize('selected,previous', [(['LAB_CEA'], ['FORGED']),
    (['FORGED'], ['LAB_CEA']), ([f'LAB_{i}' for i in range(9)], []),
    (['LAB_CEA'], [f'LAB_{i}' for i in range(9)])])
def test_hidden_order_cannot_admit_unknown_codes_or_more_than_eight(selected, previous):
    summaries = [SimpleNamespace(standard_code=code, standard_name=code)
                 for code in ['LAB_CEA', *[f'LAB_{i}' for i in range(9)]]]
    form = JointTrendForm({'code': selected, 'code_order': previous}, summaries=summaries)
    assert not form.is_valid()
