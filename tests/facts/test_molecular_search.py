import pytest

from tests.facts.molecular_factories import graph
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("query", ["NM_SYN.2", "codon 4", "build-X chr2:12"])
def test_actual_records_search_finds_full_identity_components_without_reviving_excluded_fields(django_user_model, query):
    client, patient, document, _, fields = graph(django_user_model)
    response = client.get('/records/', {'q': query})
    assert response.status_code == 200
    assert any(card.document.pk == document.pk for group in response.context['record_groups'] for card in group.cards)
    review(patient, fields['identity'], 'EXCLUDE')
    response = client.get('/records/', {'q': query})
    assert not response.context['record_groups']
    other_client, _, _, _, _ = graph(django_user_model, 'molecular-search-other')
    assert str(document.pk) not in other_client.get('/records/', {'q': query}).content.decode()
