"""Public outputs omit access strings without rewriting private source evidence."""

import io
import json
import zipfile

import pytest

from apps.exports.content import build_snapshot
from apps.exports.formats import build_artifact, csv_tables, json_bytes
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.patients.sharing_content import project_snapshot
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.test_clinical_foundation import CT, clinical_fixture


pytestmark = pytest.mark.django_db
ACCESS_URL = 'https://imaging.example.invalid/view?access=SYNTHETIC_SECRET#record'


def _fixture(django_user_model):
    texts = [*CT[:3], '诊断意见：双肺结节。云影像：' + ACCESS_URL + '。', CT[4]]
    client, patient, document, version, _ = clinical_fixture(
        django_user_model, name='cloud-public-boundary', texts=texts,
    )
    for fact in document.facts.all():
        revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0,
                    expected_source=effective_fact(fact)['current_source_token'], checked_original=True)
    private = {
        'ocr': list(version.ocr_blocks.order_by('reading_order').values('pk', 'text', 'polygon')),
        'facts': list(document.facts.order_by('pk').values('pk', 'raw_text', 'automatic_content', 'revision_number')),
        'revisions': list(document.facts.order_by('pk').values('revisions__id', 'revisions__after', 'revisions__source')),
    }
    assert any(ACCESS_URL in row['raw_text'] for row in private['facts'])
    return client, patient, document, version, private


def _private_after(document, version):
    return {
        'ocr': list(version.ocr_blocks.order_by('reading_order').values('pk', 'text', 'polygon')),
        'facts': list(document.facts.order_by('pk').values('pk', 'raw_text', 'automatic_content', 'revision_number')),
        'revisions': list(document.facts.order_by('pk').values('revisions__id', 'revisions__after', 'revisions__source')),
    }


@pytest.mark.parametrize('consumer', ['json', 'csv', 'pdf', 'zip', 'share'])
def test_normal_outputs_omit_access_parameters_and_keep_clinical_content(django_user_model, consumer):
    _, patient, document, version, private = _fixture(django_user_model)
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    store = InMemoryObjectStore()
    if consumer == 'json':
        text = json_bytes(snapshot).decode()
    elif consumer == 'csv':
        text = '\n'.join(payload.decode('utf-8-sig') for payload in csv_tables(snapshot).values())
    elif consumer == 'pdf':
        from pypdf import PdfReader

        artifact = build_artifact(snapshot, {'format': 'pdf'}, store)
        text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(artifact.payload)).pages)
    elif consumer == 'zip':
        artifact = build_artifact(snapshot, {'format': 'zip', 'parts': ['json', 'csv']}, store)
        with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
            text = '\n'.join(bundle.read(name).decode('utf-8-sig') for name in bundle.namelist()
                             if name.endswith(('.json', '.csv')))
    else:
        projected = project_snapshot(snapshot, {'document_ids': [str(document.pk)], 'sections': ['imaging']})
        text = json.dumps(projected, ensure_ascii=False)
    assert 'SYNTHETIC_SECRET' not in text
    assert 'imaging.example.invalid' not in text
    assert '双肺结节' in text
    assert '已省略外部访问内容' in text
    assert _private_after(document, version) == private


def test_omitted_original_context_has_marker_and_no_false_character_offsets(django_user_model):
    _, patient, document, version, private = _fixture(django_user_model)
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    impression = next(row for row in snapshot['clinical_fields'] if row['field_key'] == 'imaging.impression')
    sources = [row for row in snapshot['clinical_field_sources'] if row['fact_id'] == impression['id']]
    assert sources
    assert any(row.get('external_access_omitted') for row in sources)
    assert all(row['start_offset'] is None and row['end_offset'] is None for row in sources)
    assert all('SYNTHETIC_SECRET' not in row['raw_text'] for row in sources)
    assert _private_after(document, version) == private
