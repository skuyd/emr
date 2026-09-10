"""Explicit cloud selection is independent of whole-document permission."""
import json
import io
import csv
import zipfile
from copy import deepcopy
from uuid import uuid4

import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import json_bytes, read_structured_data
from .test_controlled_open import confirmed
from .test_source_services import FIRST_URL, SECOND_URL, _decide

pytestmark = pytest.mark.django_db


def source_selection(source, **changes):
    return {'mode': 'documents', 'document_ids': [], 'cloud_source_ids': [str(source.pk)], **changes}


def test_source_only_json_retains_exact_target_without_document_permission(django_user_model):
    _, patient, document, source = confirmed(django_user_model)
    snapshot = build_snapshot(patient, source_selection(source))
    assert snapshot['documents'] == snapshot['facts'] == snapshot['labs'] == []
    data = read_structured_data(json_bytes(snapshot))
    assert data['cloud_imaging_sources'][0]['current_url'] == FIRST_URL
    assert data['cloud_imaging_sources'][0]['document_id'] == str(document.pk)
    assert data['cloud_imaging_evidence'][0]['source_id'] == str(source.pk)
    assert data['cloud_imaging_evidence'][0]['start_offset'] is None
    # Other domains can describe omitted history in schema notes; no actual
    # history member may be present anywhere in the public object.
    def has_history(value):
        if isinstance(value, dict):
            return 'history' in value or any(has_history(item) for item in value.values())
        return isinstance(value, list) and any(has_history(item) for item in value)
    assert not has_history(data)


def test_default_document_selection_never_grants_cloud_output(django_user_model):
    _, patient, document, source = confirmed(django_user_model)
    snapshot = build_snapshot(patient, {'mode': 'documents', 'document_ids': [str(document.pk)]})
    data = read_structured_data(json_bytes(snapshot))
    assert data['cloud_imaging_sources'] == data['cloud_imaging_evidence'] == []
    assert FIRST_URL not in json_bytes(snapshot).decode()


def test_old_expected_source_token_cannot_confirm_new_target_for_output(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    old = source.confirmed_fingerprint
    source = _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
    with pytest.raises(ExportInputError):
        build_snapshot(patient, source_selection(source, cloud_source_tokens={str(source.pk): old}))


def test_source_only_actual_pdf_csv_zip_have_selected_target_and_empty_originals(django_user_model):
    from pypdf import PdfReader
    from apps.exports.formats import build_artifact, csv_tables
    from apps.exports.pdf import render_pdf
    from tests.documents.fakes import InMemoryObjectStore
    _, patient, _, source = confirmed(django_user_model)
    snapshot=build_snapshot(patient, source_selection(source, sections=['cloud_imaging'], details=True))
    text=''.join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(snapshot))).pages)
    assert FIRST_URL in ''.join(text.split())
    tables=csv_tables(snapshot)
    sources=list(csv.DictReader(io.StringIO(tables['cloud_imaging_sources.csv'].decode('utf-8-sig'))))
    assert sources[0]['current_url']==FIRST_URL
    assert list(csv.DictReader(io.StringIO(tables['documents.csv'].decode('utf-8-sig'))))==[]
    artifact=build_artifact(snapshot, {'format':'zip','parts':['json','csv','pdf']}, InMemoryObjectStore())
    try:
        with zipfile.ZipFile(artifact.stream) as bundle:
            assert not any(name.startswith('originals/') for name in bundle.namelist())
            manifest=json.loads(bundle.read('manifest.json'))
            assert manifest['document_ids']==[] and manifest['cloud_source_ids']==[str(source.pk)]
            assert json.loads(bundle.read('records.json'))['cloud_imaging_sources'][0]['current_url']==FIRST_URL
    finally:
        artifact.close()


@pytest.mark.parametrize('mutation', ['missing_table','unknown_source','wrong_evidence','extra_field','bool_revision','manual_offsets','url_title','false_omission_flag','unselected'])
def test_reader_rejects_malformed_selected_cloud_relations(django_user_model, mutation):
    _, patient, _, source = confirmed(django_user_model)
    data=json.loads(json_bytes(build_snapshot(patient, source_selection(source))))
    if mutation=='missing_table':data.pop('cloud_imaging_evidence')
    elif mutation=='unknown_source':data['cloud_imaging_sources'][0]['id']=str(uuid4())
    elif mutation=='wrong_evidence':data['cloud_imaging_evidence'][0]['id']=str(uuid4())
    elif mutation=='extra_field':data['cloud_imaging_sources'][0]['history']=[FIRST_URL]
    elif mutation=='bool_revision':data['cloud_imaging_sources'][0]['revision_number']=True
    elif mutation=='manual_offsets':data['cloud_imaging_evidence'][0]['start_offset']=0
    elif mutation=='url_title':data['cloud_imaging_sources'][0].update(title=SECOND_URL,external_access_omitted=True)
    elif mutation=='false_omission_flag':data['cloud_imaging_sources'][0]['external_access_omitted']='true'
    else:data['scope']['cloud_source_ids']=[]
    with pytest.raises(ExportInputError):read_structured_data(json.dumps(data))


@pytest.mark.parametrize('version',['1.0','1.1','1.2','1.3','1.4'])
def test_published_older_formats_default_missing_cloud_tables_to_empty(version):
    from apps.exports.treatment import ARRAYS
    data={'schema_version':version, **{key:[] for key in ('documents','facts','labs','sources','clinical_reports','clinical_fields','clinical_field_sources','self_records','glucose_records','glucose_record_sources',*ARRAYS)}}
    current=read_structured_data(json.dumps(data))
    assert current['cloud_imaging_sources']==current['cloud_imaging_evidence']==[]
