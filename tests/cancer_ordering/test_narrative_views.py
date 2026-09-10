"""Private review of actual uploaded narrative sources, without a fabricated Fact."""
from html import unescape
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
import pytest

from apps.cancer_ordering.models import (
    CancerCandidate, CandidateRevision, CollectionCandidate, CollectionRun,
    DisplaySelection, NarrativeDependency, NarrativeSource, SelectionRevision,
)
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.documents.models import Document, ProcessingRun
from apps.facts.models import ClinicalReport, ClinicalReportRevision, Fact, FactRevision
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientMembership
from apps.processing.models import OcrBlock, ParsingVersion
from apps.processing.runner import ExecutionState, run_processing
from tests.cancer_ordering.test_pipeline import _page
from tests.cancer_ordering.test_views import _decision, _url
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.documents.test_upload_views import reserve_one, upload_path
from tests.processing.test_pipeline import _pipeline, _png_bytes


pytestmark = pytest.mark.django_db(transaction=True)
CONTEXT = '主诉：肺癌治疗后不适。无发热。'


@pytest.fixture
def uploaded_narrative(django_user_model, monkeypatch, settings):
    cache.clear()
    settings.PROCESSING_DISPATCH_ON_UPLOAD = False
    store = InMemoryObjectStore()
    monkeypatch.setattr('apps.documents.views.uploads.get_object_store', lambda: store)
    monkeypatch.setattr('apps.documents.views.originals.get_object_store', lambda: store)

    def upload(*lines):
        client, patient = _patient(django_user_model, 'narrative-private-page')
        payload = _png_bytes()  # Same 100 x 100 dimensions as the fixed OCR page.
        batch_id, item_id = reserve_one(client, name='synthetic-narrative.png', byte_size=len(payload))
        response = client.post(upload_path(batch_id, item_id), {
            'file': SimpleUploadedFile('synthetic-narrative.png', payload, content_type='image/png')})
        assert response.status_code == 201
        document = Document.objects.get(pk=response.json()['document_id'])
        run = ProcessingRun.objects.get(document=document)
        result = run_processing(run.pk, _pipeline(store, _page(*(lines or (CONTEXT,)))))
        run.refresh_from_db()
        assert result.state == ExecutionState.SUCCEEDED, run.error_code
        candidate = CancerCandidate.objects.get(patient=patient)
        assert candidate.source_narrative_id and candidate.source_fact_id is None and candidate.source_report_id is None
        return client, patient, document, candidate
    return upload


def _stored_material():
    # Read auditing/session updates are legitimate; source and review writes are not.
    models = (CancerCandidate, CandidateRevision, CollectionCandidate, CollectionRun,
              DisplaySelection, NarrativeDependency, NarrativeSource, SelectionRevision,
              Fact, FactRevision, ClinicalReport, ClinicalReportRevision, OcrBlock, ParsingVersion)
    return {model._meta.label: list(model.objects.order_by('pk').values()) for model in models}


def _section(html, identity):
    match = re.search(r'<section\b[^>]*\bid="' + identity + r'"[^>]*>.*?</section>', html, re.S)
    assert match, f'Missing separately labelled review section: {identity}'
    return unescape(match.group())


class _Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.hrefs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs).get('href', ''))


def test_uploaded_narrative_detail_has_real_page_full_context_and_no_read_writes(uploaded_narrative):
    client, patient, document, candidate = uploaded_narrative()
    assert not Fact.objects.filter(document=document).exists()
    assert not ClinicalReport.objects.filter(document=document).exists()
    before = _stored_material()
    response = client.get(_url({'id': str(candidate.pk)}))
    assert response.status_code == 200
    html = response.content.decode()
    original = _section(html, 'candidate-original')
    source = _section(html, 'candidate-source')
    assert '原始采集快照' in original and '肺癌治疗后不适' in original
    assert '原始字面片段' in original and '肺癌' in original
    assert '来源栏目：主诉' in source and CONTEXT in source
    assert '核对' in source and '所属对象' in source
    links = _Links(html).hrefs
    viewer = next(link for link in links if urlsplit(link).path == reverse('documents:document_viewer', args=[document.pk]))
    assert parse_qs(urlsplit(viewer).query) == {'page': ['1'], 'patient': [str(patient.pk)]}
    assert not any(urlsplit(link).path.startswith('/facts/') for link in links)
    assert client.get(viewer).status_code == 200
    assert 'no-store' in response['Cache-Control']
    assert _stored_material() == before


def test_manual_current_value_does_not_replace_narrative_snapshot_or_context(uploaded_narrative):
    client, patient, _, candidate = uploaded_narrative()
    original = candidate.original_data.copy()
    row, = candidate_rows(patient)
    response = client.post(_url(row), _decision(patient, row, action='CORRECT', label='胰腺癌',
        profile='PANCREAS', assertion='UNCERTAIN', subject='HISTORICAL', reason='<script>synthetic</script>'))
    assert response.status_code == 303
    before = _stored_material()
    response = client.get(_url(row))
    assert response.status_code == 200
    html = response.content.decode()
    snapshot = _section(html, 'candidate-original')
    source = _section(html, 'candidate-source')
    current = _section(html, 'candidate-current')
    assert '肺癌' in snapshot and '胰腺癌' not in snapshot
    assert CONTEXT in source and '胰腺癌' not in source
    assert '当前人工更正' in current and '胰腺癌' in current and '疑似或待排' in current and '既往病史' in current
    assert '核对历史' in html and '&lt;script&gt;synthetic' in html and '<script>synthetic' not in html
    candidate.refresh_from_db()
    assert candidate.original_data == original and _stored_material() == before


@pytest.mark.parametrize('action,notice', [
    ('CONFIRM', '变化'), ('CORRECT', '变化'), ('EXCLUDE', '已排除'), ('DEFER', '暂不处理'),
])
def test_narrative_page_explains_current_parent_review_without_fake_fact_link(uploaded_narrative, action, notice):
    client, patient, document, candidate = uploaded_narrative('现病史：患者诊断为肺癌，已行化疗。')
    parent = Fact.objects.get(document=document)
    assert parent.category == 'TREATMENT'
    revise_fact(patient, parent.pk, actor=patient.account, action=action, expected_revision=0,
                checked_original=True, changes={'text': '现病史：患者诊断为胰腺癌，已行化疗。'} if action == 'CORRECT' else None)
    before = _stored_material()
    response = client.get(_url({'id': str(candidate.pk)}))
    assert response.status_code == 200
    html = response.content.decode()
    source = _section(html, 'candidate-source')
    assert '来源栏目：现病史' in source and notice in source and '原文摘录' in source
    assert '核对' in source and '当前来源' in source
    assert not any(urlsplit(link).path.startswith('/facts/') for link in _Links(html).hrefs)
    assert '待核对' in html and _stored_material() == before


def test_invalid_narrative_post_retains_context_without_saving_a_review(uploaded_narrative):
    client, patient, _, _ = uploaded_narrative()
    row, = candidate_rows(patient)
    before = _stored_material()
    response = client.post(_url(row), _decision(patient, row, checked_original=''))
    assert response.status_code == 400
    assert CONTEXT in _section(response.content.decode(), 'candidate-source')
    assert response.context['form'].errors.get('checked_original')
    assert _stored_material() == before


@pytest.mark.parametrize('invalid_post', [False, True])
def test_narrative_context_is_discarded_if_parent_changes_after_render(uploaded_narrative, monkeypatch, invalid_post):
    from apps.cancer_ordering import views

    text = '现病史：患者诊断为肺癌，已行化疗。'
    client, patient, document, _ = uploaded_narrative(text)
    parent = Fact.objects.get(document=document)
    row, = candidate_rows(patient)
    original_render = views.render

    def change_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        revise_fact(patient, parent.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
        return response

    monkeypatch.setattr(views, 'render', change_after_render)
    response = (client.post(_url(row), _decision(patient, row, checked_original='')) if invalid_post
                else client.get(_url(row)))
    assert response.status_code == 409 and text not in response.content.decode()
    assert not CandidateRevision.objects.exists()


def test_narrative_detail_keeps_viewer_and_foreign_patient_boundaries(uploaded_narrative, django_user_model):
    _, patient, document, candidate = uploaded_narrative()
    outsider, other = _patient(django_user_model, 'narrative-private-outsider')
    path = _url({'id': str(candidate.pk)})
    assert outsider.get(path).status_code == 404
    membership = PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    before = _stored_material()
    response = outsider.get(path)
    assert response.status_code == 200 and '只读成员' in response.content.decode()
    assert CONTEXT in response.content.decode() and 'name="expected_revision"' not in response.content.decode()
    row, = candidate_rows(patient)
    assert outsider.post(path, _decision(patient, row)).status_code == 403
    assert outsider.get(path, {'patient': str(other.pk)}).status_code == 404
    assert _stored_material() == before
    membership.delete()
    assert outsider.get(path).status_code == 404
    assert outsider.get(reverse('documents:document_viewer', args=[document.pk]), {'patient': str(patient.pk)}).status_code == 404


def test_unproved_narrative_subject_is_still_displayed_as_unproved(uploaded_narrative):
    client, patient, _, candidate = uploaded_narrative('现病史：肺癌。')
    response = client.get(_url({'id': str(candidate.pk)}))
    assert response.status_code == 200
    current = _section(response.content.decode(), 'candidate-current')
    assert '所属对象不明确' in current and '待核对' in current
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    assert not CandidateRevision.objects.exists()
