"""Real output selection keeps typed dependency material private and live."""
import csv
import hashlib
import io
import json
import zipfile

from pypdf import PdfReader
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportUnavailable, SnapshotChanged
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import render_pdf
from apps.exports.services import create_preview, get_preview
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import review
from tests.patients.test_family_shares import exchange

pytestmark = pytest.mark.django_db


def setup(django_user_model, *, field_selected=False):
    client, patient, document, _, field, anchor = typed_fixture(django_user_model)
    if field_selected:
        review(patient, field)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    selection = selected_body(patient, cancer_candidate_ids=[str(candidate.pk)])
    if field_selected:
        selection.update(document_ids=[str(document.pk)], clinical_field_ids=[str(field.pk)],
                         sections=['imaging', 'cancer_ordering'])
    return client, patient, document, field, anchor, selection


def test_candidate_alone_all_output_formats_omit_typed_private_graph(django_user_model):
    _, patient, _, field, anchor, selection = setup(django_user_model)
    snapshot = build_snapshot(patient, selection)
    item, = snapshot['cancer_candidates']
    assert item['source']['state'] == 'OMITTED'
    assert snapshot['clinical_fields'] == snapshot['clinical_reports'] == snapshot['documents'] == []
    portable = json_bytes(snapshot)
    assert read_structured_data(portable.decode())['cancer_candidates'] == [item]
    tables = csv_tables(snapshot)
    rows = list(csv.DictReader(io.StringIO(tables['cancer_candidates.csv'].decode('utf-8-sig'))))
    assert rows[0]['label'] == '肺癌' and json.loads(rows[0]['source'])['state'] == 'OMITTED'
    pdf = render_pdf(snapshot)
    pdf_text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert '肺癌' in pdf_text and '待核对' in pdf_text
    private = [str(field.pk), str(anchor.pk), str(patient.account_id), 'dependency_heads', 'TYPED_HISTOLOGY']
    for forbidden in private:
        assert forbidden not in portable.decode() and forbidden not in pdf_text
    with build_artifact(snapshot, {'format': 'zip', 'parts': ['pdf', 'json', 'csv']}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            for member in manifest['files']:
                content = archive.read(member['path'])
                assert hashlib.sha256(content).hexdigest() == member['sha256']
                if member['path'].endswith(('.json', '.csv')):
                    assert all(value.encode() not in content for value in private)


def test_explicit_selected_histology_gets_only_actual_minimal_source_reference(django_user_model):
    _, patient, document, field, anchor, selection = setup(django_user_model, field_selected=True)
    snapshot = build_snapshot(patient, selection)
    source = snapshot['cancer_candidates'][0]['source']
    assert source == {'state': 'SELECTED_REFERENCE', 'document_id': str(document.pk),
        'fact_id': str(field.pk), 'page': field.document_page.page_number, 'location': 'PAGE'}
    assert [row['id'] for row in snapshot['clinical_fields']] == [str(field.pk)]
    data = read_structured_data(json_bytes(snapshot).decode())
    assert data['cancer_candidates'][0]['source'] == source
    assert str(anchor.pk) not in json.dumps(data)
    selection['clinical_field_ids'] = []
    assert build_snapshot(patient, selection)['cancer_candidates'][0]['source']['state'] == 'OMITTED'


def test_unselected_anchor_review_change_invalidates_preview_and_shared_candidate(django_user_model):
    owner, patient, document, _, anchor, selection = setup(django_user_model)
    viewer, _ = _patient(django_user_model, 'typed-output-viewer')
    assert owner.get('/records/').status_code == 200
    job = create_preview(patient, owner.session.session_key, selection, actor=patient.account)
    created = create_share(patient, patient.account, selection)
    share_id = exchange(viewer, created.token)
    page = viewer.get(f'/shared/{share_id}/')
    assert page.status_code == 200 and '肺癌' in page.content.decode()
    assert str(anchor.pk) not in page.content.decode()
    assert viewer.get(f'/shared/{share_id}/documents/{document.pk}/').status_code == 404
    review(patient, anchor, 'DEFER')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, job.snapshot)
    with pytest.raises(ExportUnavailable):
        get_preview(patient, owner.session.session_key, job.pk, actor=patient.account)
    assert viewer.get(f'/shared/{share_id}/').status_code == 410
    job.refresh_from_db()
    created.share.refresh_from_db()
    assert job.snapshot == {} and created.share.snapshot == {}
