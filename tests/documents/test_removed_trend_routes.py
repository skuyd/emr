from datetime import date

import pytest

from tests.documents.test_detail_viewer import _patient
from tests.labs.helpers import _observation


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('path', ['/trends/', '/trends/compare/', '/trends/LAB_WBC/'])
def test_removed_trend_routes_return_not_found(django_user_model, path):
    client, patient = _patient(django_user_model, 'removed-trend')
    document, _ = _observation(patient, date(2026, 7, 1), '4.2')
    _observation(patient, date(2026, 8, 1), '4.6')
    assert client.get(path).status_code == 404
    detail = client.get(f'/records/{document.pk}/')
    assert detail.status_code == 200
    assert '/trends/' not in detail.content.decode()


@pytest.mark.parametrize('path', ['/labs/compare/'])
def test_ordered_pages_open_with_adjacent_ocr_section_boxes(django_user_model, path):
    from tests.facts.factories import parsed_facts

    client, patient = _patient(django_user_model, 'index-ocr-gap-' + path)
    _observation(patient, date(2026, 7, 1), '4.2')
    _observation(patient, date(2026, 8, 1), '4.6')
    _, version = parsed_facts(patient, ['主诉：头痛。', '查体：合成检查。'])
    for index, block in enumerate(version.ocr_blocks.order_by('reading_order')):
        left, right = ((.08, .28), (.29, .54))[index]
        block.polygon = [[left, .10], [right, .10], [right, .125], [left, .125]]
        block.save(update_fields=['polygon'])

    response = client.get(path)

    assert response.status_code == 200
    assert '白细胞' in response.content.decode()
    assert response['Cache-Control'] == 'private, no-store, max-age=0'
