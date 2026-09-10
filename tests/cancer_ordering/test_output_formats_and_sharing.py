from copy import deepcopy
import csv
import hashlib
import io
import json
import uuid
import zipfile

from pypdf import PdfReader
import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import render_pdf
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.patients.test_family_shares import exchange


pytestmark = pytest.mark.django_db


def selected_snapshot(django_user_model, name):
    client, patient = _patient(django_user_model, name)
    _collect(patient)
    _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    selection = selected_body(patient, cancer_candidate_ids=[_row(patient)['id']], include_indicator_ordering=True)
    return client, patient, build_snapshot(patient, selection)


def test_actual_pdf_csv_and_zip_contain_only_explicit_current_statements_and_display_choice(django_user_model):
    _, patient, snapshot = selected_snapshot(django_user_model, 'cancer-output-formats')
    pdf = render_pdf(snapshot)
    text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert '肺癌' in text and '待核对' in text and '胰腺癌指标顺序' in text and '手动' in text
    tables = csv_tables(snapshot)
    candidates = list(csv.DictReader(io.StringIO(tables['cancer_candidates.csv'].decode('utf-8-sig'))))
    assert len(candidates) == 1 and candidates[0]['label'] == '肺癌'
    assert json.loads(candidates[0]['source'])['state'] == 'OMITTED'
    choices = list(csv.DictReader(io.StringIO(tables['indicator_ordering.csv'].decode('utf-8-sig'))))
    assert len(choices) == 1 and choices[0]['mode'] == 'MANUAL_PROFILE'
    with build_artifact(snapshot, {'format': 'zip', 'parts': ['pdf', 'json', 'csv']}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            for member in manifest['files']:
                content = archive.read(member['path'])
                assert len(content) == member['byte_size'] and hashlib.sha256(content).hexdigest() == member['sha256']
                if member['path'].endswith('.json') or member['path'].endswith('.csv'):
                    assert b'cancer_ordering_fingerprint' not in content and str(patient.account_id).encode() not in content
            assert manifest['cancer_candidate_ids'] == [snapshot['cancer_candidates'][0]['id']]
            assert manifest['include_indicator_ordering'] is True
            assert not any(name.startswith('originals/') for name in archive.namelist())


@pytest.mark.parametrize('change', ['old_raw_text', 'bad_status', 'unknown_mode', 'unselected_candidate', 'unselected_source'])
def test_portable_reader_rejects_invalid_or_dangling_new_material(django_user_model, change):
    _, _, snapshot = selected_snapshot(django_user_model, 'cancer-output-invalid-' + change)
    data = json.loads(json_bytes(snapshot))
    if change == 'old_raw_text':
        data['cancer_candidates'][0]['original_data'] = {'raw': 'must not be silently admitted'}
    elif change == 'bad_status':
        data['cancer_candidates'][0]['status'] = 'EXCLUDED'
    elif change == 'unknown_mode':
        data['indicator_ordering'][0]['mode'] = 'INFERRED_DIAGNOSIS'
    elif change == 'unselected_candidate':
        data['indicator_ordering'][0].update(mode='CANDIDATE', candidate_id=str(uuid.uuid4()))
    else:
        data['cancer_candidates'][0]['source'] = {'state': 'SELECTED_REFERENCE', 'document_id': str(uuid.uuid4()),
            'fact_id': str(uuid.uuid4()), 'page': 1, 'location': 'REGION'}
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


@pytest.mark.parametrize('version', ['1.0', '1.1', '1.2', '1.3', '1.4', '1.5'])
def test_previous_portable_versions_need_no_cancer_arrays_and_are_not_modified(django_user_model, version):
    _, _, snapshot = selected_snapshot(django_user_model, 'cancer-output-old-' + version)
    data = json.loads(json_bytes(snapshot))
    data['schema_version'] = version
    del data['cancer_candidates'], data['indicator_ordering']
    payload = json.dumps(data)
    before = deepcopy(data)
    result = read_structured_data(payload)
    assert result['cancer_candidates'] == result['indicator_ordering'] == []
    assert data == before and result['schema_version'] == version


@pytest.mark.parametrize('change', ['mode_reason', 'candidate_profile', 'candidate_assertion', 'candidate_subject'])
def test_reader_rejects_display_basis_that_contradicts_the_carried_statement(django_user_model, change):
    _, _, snapshot = selected_snapshot(django_user_model, 'cancer-output-coherence-' + change)
    data = json.loads(json_bytes(snapshot))
    candidate = data['cancer_candidates'][0]
    choice = data['indicator_ordering'][0]
    choice.update(mode='CANDIDATE', profile='LUNG', reason='selected_reported_diagnosis', candidate_id=candidate['id'])
    assert read_structured_data(json.dumps(data))['indicator_ordering'][0] == choice
    if change == 'mode_reason':
        choice.update(mode='MANUAL_PROFILE', profile='GENERAL', reason='collection_incomplete', candidate_id=None)
    elif change == 'candidate_profile':
        choice['profile'] = 'PANCREAS'
    elif change == 'candidate_assertion':
        candidate['assertion'] = 'NEGATED'
    else:
        candidate['subject'] = 'OTHER_PERSON'
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


def test_actual_shared_candidate_does_not_open_its_original_and_new_uncollected_input_scrubs_it(django_user_model):
    owner, patient = _patient(django_user_model, 'cancer-share-owner')
    viewer, _ = _patient(django_user_model, 'cancer-share-reader')
    document, _ = _collect(patient)
    assert owner.get('/records/').status_code == 200
    selected = selected_body(patient, cancer_candidate_ids=[_row(patient)['id']])
    created = create_share(patient, patient.account, selected)
    share_id = exchange(viewer, created.token)
    page = viewer.get(f'/shared/{share_id}/')
    assert page.status_code == 200
    assert '肺癌' in page.content.decode() and '待核对' in page.content.decode()
    assert created.share.snapshot['documents'] == [] and not created.share.source_bindings.exists()
    assert str(patient.account_id) not in page.content.decode() and 'cancer_ordering_fingerprint' not in page.content.decode()
    assert viewer.get(f'/shared/{share_id}/documents/{document.pk}/').status_code == 404
    assert viewer.get(f'/records/{document.pk}/').status_code == 404
    parsed_facts(patient, ['出院诊断：胰腺癌。'])
    assert viewer.get(f'/shared/{share_id}/').status_code == 410
    created.share.refresh_from_db()
    assert created.share.invalidated_at is not None and created.share.snapshot == {}
