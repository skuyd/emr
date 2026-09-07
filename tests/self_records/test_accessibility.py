from html.parser import HTMLParser
from uuid import uuid4

import pytest

from apps.self_records.services import create_record, revise_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload
from tests.self_records.test_views import form_data


pytestmark = pytest.mark.django_db


class Markup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.nodes = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def assert_accessible_references(response):
    markup = Markup(response.content.decode())
    ids = [attrs['id'] for _, attrs in markup.nodes if attrs.get('id')]
    assert len(ids) == len(set(ids))
    labels = {attrs['for'] for tag, attrs in markup.nodes if tag == 'label' and attrs.get('for')}
    for tag, attrs in markup.nodes:
        for reference in ('aria-describedby', 'aria-labelledby'):
            assert set(attrs.get(reference, '').split()) <= set(ids), attrs
        if tag in {'input', 'select', 'textarea'} and attrs.get('type') not in {'hidden', 'submit', 'button'}:
            assert attrs.get('id') in labels or attrs.get('aria-label') or attrs.get('aria-labelledby'), attrs
    return markup


@pytest.mark.parametrize('kind', ['WEIGHT', 'TEMPERATURE', 'SYMPTOM'])
def test_quick_form_labels_help_and_selected_kind_are_announced(django_user_model, kind):
    _, patient, client, _, _ = family(django_user_model, 'daily-markup-' + kind)
    response = client.get('/self-records/new/', {'patient': str(patient.pk), 'kind': kind})
    assert response.status_code == 200
    markup = assert_accessible_references(response)
    links = [attrs for tag, attrs in markup.nodes if tag == 'a' and 'kind=' in attrs.get('href', '')]
    current = [attrs for attrs in links if attrs.get('aria-current') == 'page']
    assert len(current) == 1 and 'kind=' + kind in current[0]['href']


def test_error_and_revised_history_have_unique_ids_and_readable_chart_sources(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-markup-history')
    invalid = client.post('/self-records/new/', form_data(patient, value='NaN'))
    assert invalid.status_code == 400
    assert_accessible_references(invalid)
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
    detail = client.get(f'/self-records/{record.pk}/')
    assert detail.status_code == 200
    assert_accessible_references(detail)
    index = client.get('/self-records/', {'patient': str(patient.pk)})
    assert index.status_code == 200
    markup = assert_accessible_references(index)
    chart = next(attrs for tag, attrs in markup.nodes if tag == 'svg' and attrs.get('role') == 'img')
    assert chart['aria-labelledby'] and chart['aria-describedby']
    assert any(tag == 'ol' and attrs.get('id') == chart['aria-describedby'] for tag, attrs in markup.nodes)
