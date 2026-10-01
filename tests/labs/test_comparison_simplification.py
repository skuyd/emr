from datetime import date
import re

import pytest

from tests.documents.test_detail_viewer import _patient
from tests.labs.helpers import _observation


pytestmark = pytest.mark.django_db


def test_comparison_keeps_filtered_results_without_extra_columns_or_navigation(django_user_model):
    client, patient = _patient(django_user_model, 'comparison-simplified')
    for day in (1, 2, 3):
        _observation(patient, date(2026, 8, day), str(day))

    response = client.get('/labs/compare/', {
        'patient': patient.pk, 'start': '2026-08-02', 'end': '2026-08-03', 'project': '白细胞',
    })

    assert response.status_code == 200
    assert len(response.context['comparison'].rows) == 1
    assert [column.date_label for column in response.context['comparison'].columns] == [
        '2026-08-02', '2026-08-03',
    ]
    html = response.content.decode()
    header = re.search(r'<thead.*?</thead>', html, re.S).group()
    assert len(re.findall(r'<th\b', header)) == 3
    assert 'data-show-trends' not in html
    assert 'aria-label="检验工作区"' not in html
    assert '打开多指标对照' not in html
    assert '同一采样日期、同一医院的结果合列' not in html
    assert '琥珀色点线数值' not in html
    assert '白细胞' in html and '10^9/L' in html
    assert 'comparison-value' in html


def test_comparison_keeps_unit_with_indicator_when_reference_is_missing(django_user_model):
    client, patient = _patient(django_user_model, 'comparison-unit-without-reference')
    row = _observation(patient, date(2026, 8, 1), '5', raw_name='目录外合成项目')[1]
    row.reference_range_raw = ''
    row.save(update_fields=['reference_range_raw'])
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    name = re.search(r'<th scope="row">.*?</th>', response.content.decode(), re.S).group()
    assert '10^9/L' in name
