"""Behavior contracts for explicitly selected glucose output and limited sharing."""

import csv
import io
import json
from uuid import uuid4
import zipfile
from copy import deepcopy

from pypdf import PdfReader
import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, ExportUnavailable
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import render_pdf
from apps.exports.services import create_preview, get_preview
from apps.glucose.services import create_record, revise_record
from apps.patients.sharing import create_share, exchange_share_token
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.glucose.factories import lab_source
from tests.glucose.test_payloads import payload
from tests.glucose.test_sources import import_source
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def selection(record):
    return {'mode': 'documents', 'document_ids': [], 'glucose_record_ids': [str(record.pk)],
            'sections': ['patient', 'glucose'], 'details': True}


def test_record_only_json_csv_pdf_zip_preserve_selected_revision_and_original_quantity(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-portable-selected')
    record = create_record(patient, actor, payload(value='100', unit='mg/dL', notes='=selected note'),
                           creation_key=uuid4(), source_kind='METER').record
    create_record(patient, actor, payload(notes='PRIVATE UNSELECTED ENTRY'), creation_key=uuid4())
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                  changes=payload(value='110', unit='mg/dL', notes='=selected current note'))
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    data = json.loads(json_bytes(job.snapshot))
    assert data['schema_version'] == '1.4'
    assert data['scope']['glucose_record_ids'] == [str(record.pk)]
    assert data['documents'] == data['facts'] == data['labs'] == data['self_records'] == []
    assert len(data['glucose_records']) == len(data['glucose_record_sources']) == 1
    row = data['glucose_records'][0]
    assert row['id'] == str(record.pk) and row['data']['raw_value'] == '110'
    assert row['data']['raw_unit'] == 'mg/dL' and row['data']['normalized_value'] == '6.1061'
    assert row['original_data']['raw_value'] == '100'
    assert row['created_by'] == row['updated_by'] == row['revision_author'] == str(actor.pk)
    assert row['revision_id'] and row['revision_number'] == 1
    assert job.glucose_sources.get().record_id == record.pk
    assert 'PRIVATE UNSELECTED ENTRY' not in json.dumps(data)
    assert read_structured_data(json_bytes(job.snapshot))['glucose_records'] == data['glucose_records']
    tables = csv_tables(job.snapshot)
    records = list(csv.DictReader(io.StringIO(tables['glucose_records.csv'].decode('utf-8-sig'))))
    assert len(records) == 1 and records[0]['id'] == str(record.pk)
    assert records[0]['raw_value'] == '110' and records[0]['raw_unit'] == 'mg/dL'
    assert records[0]['normalized_value'] == '6.1061' and records[0]['notes'] == "'=selected current note"
    assert json.loads(records[0]['original_data'])['raw_value'] == '100'
    assert json.loads(records[0]['conversion'])['factor'] == '0.05551'
    text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(job.snapshot))).pages)
    assert all(value in text for value in ('110 mg/dL', '6.1061 mmol/L', '2026-09-08T08:25', '血糖', '分钟'))
    assert 'PRIVATE UNSELECTED ENTRY' not in text
    artifact = build_artifact(job.snapshot, {'format': 'zip', 'parts': ['json', 'csv', 'pdf']}, InMemoryObjectStore())
    with artifact, zipfile.ZipFile(artifact.stream) as archive:
        assert {'records.json', 'csv/glucose_records.csv', 'csv/glucose_record_sources.csv', 'visit-card.pdf'} <= set(archive.namelist())
        assert not any(path.startswith('originals/') for path in archive.namelist())
        assert json.loads(archive.read('manifest.json'))['glucose_record_ids'] == [str(record.pk)]


def test_report_glucose_only_keeps_source_binding_without_whole_document_permission(django_user_model):
    client, patient, document, version, observation = lab_source(django_user_model, marker='glucose-export-source-only')
    record = import_source(patient, observation).record
    assert client.get('/records/').status_code == 200
    job = create_preview(patient, client.session.session_key, selection(record), actor=patient.account)
    data = json.loads(json_bytes(job.snapshot))
    assert data['documents'] == data['facts'] == data['labs'] == []
    assert job.snapshot['glucose_document_ids'] == [str(document.pk)]
    assert job.glucose_sources.get().record.source_document_id == document.pk
    source = data['glucose_record_sources'][0]
    assert source['document_id'] == str(document.pk) and source['observation_id'] == str(observation.pk)
    assert source['sampling']['local'] == '2026-08-02T06:12:34'
    assert source['sampling']['timezone_origin'] == 'UNCONFIRMED'
    assert source['reporting']['local'] == '2026-08-02T09:24:56'
    assert data['glucose_records'][0]['data']['measured_at'] is None
    with pytest.raises(ExportInputError):
        build_artifact(job.snapshot, {'format': 'original'}, InMemoryObjectStore())
    from apps.documents.lifecycle import move_to_trash
    move_to_trash(patient, document.pk, actor=patient.account)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}


def test_limited_glucose_share_exposes_current_values_and_revokes_after_revision(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-limited-sharing')
    record = create_record(patient, actor, payload(notes='PRIVATE ORIGINAL NOTE'), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                  changes=payload(value='6.75', notes='selected current note'))
    create_record(patient, actor, payload(notes='PRIVATE UNSELECTED NOTE'), creation_key=uuid4())
    created = create_share(patient, patient.account, selection(record))
    row = created.share.snapshot['glucose_records'][0]
    assert row['data']['raw_value'] == '6.75'
    assert not {'created_by', 'updated_by', 'revision_author', 'original_data', 'revision_id'} & set(row)
    assert str(actor.pk) not in json.dumps(row)
    assert created.share.glucose_sources.get().record_id == record.pk
    reader, own = _patient(django_user_model, 'glucose-limited-reader')
    assert reader.get('/shared/open/').status_code == 200
    exchange_share_token(created.token, own.account, reader.session.session_key)
    response = reader.get(f'/shared/{created.share.pk}/')
    text = response.content.decode()
    assert response.status_code == 200 and 'selected current note' in text and '6.75' in text
    assert 'PRIVATE ORIGINAL NOTE' not in text and 'PRIVATE UNSELECTED NOTE' not in text
    assert '/glucose/' not in text
    assert reader.get(f'/glucose/{record.pk}/').status_code == 404
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=1)
    created.share.refresh_from_db()
    assert created.share.invalidated_at is not None and created.share.snapshot == {}
    assert reader.get(f'/shared/{created.share.pk}/').status_code == 410


def test_glucose_revision_invalidates_frozen_export_and_undo_does_not_reopen_it(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-export-revision-wired')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.20'))
    job.refresh_from_db()
    assert job.snapshot == {} and job.status == 'INVALIDATED' and job.cleanup_pending
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=actor)
    revise_record(patient, actor, record.pk, action='UNDO', expected_revision=1)
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=actor)


def test_export_and_share_forms_accept_explicit_glucose_without_documents(django_user_model):
    owner, patient, client, actor, _ = family(django_user_model, 'glucose-output-forms')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    page = client.get('/visit/', {'patient': str(patient.pk)})
    assert page.status_code == 200 and 'glucose_record_ids' in page.context['form'].fields
    response = client.post('/visit/', {**selection(record), 'patient_id': str(patient.pk), 'nickname': '合成患者',
                                      'details': 'on', 'action': 'preview'})
    assert response.status_code == 302
    preview = client.get(response.url)
    assert preview.status_code == 200 and '5.50 mmol/L' in preview.content.decode()
    response = owner.post(f'/patients/{patient.pk}/shares/', {**selection(record), 'expires_in_hours': 24})
    assert response.status_code == 201


def test_removing_glucose_binding_fails_closed_without_changing_selected_values(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'glucose-binding-loss')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    job.glucose_sources.all().delete()
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=actor)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}


@pytest.mark.parametrize('version', ['1.0', '1.1', '1.2', '1.3'])
def test_prior_portable_versions_keep_all_their_tables_and_default_only_new_glucose_arrays(django_user_model, version):
    from apps.exports.treatment import ARRAYS

    _, patient = _patient(django_user_model, 'glucose-reader-compat-' + version)
    record = create_record(patient, patient.account, payload(), creation_key=uuid4()).record
    current = json.loads(json_bytes(build_snapshot(patient, selection(record))))
    old = deepcopy(current)
    old['schema_version'] = version
    old.pop('glucose_records')
    old.pop('glucose_record_sources')
    restored = read_structured_data(json.dumps(old))
    assert restored['glucose_records'] == restored['glucose_record_sources'] == []
    for key in ('documents', 'facts', 'labs', 'sources', 'clinical_reports', 'clinical_fields', 'clinical_field_sources', 'self_records', *ARRAYS):
        assert restored[key] == old[key]
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps({**old, 'schema_version': '1.4'}))
    assert 'glucose_records' not in old and old['schema_version'] == version


def test_glucose_mixed_with_selected_clinical_lab_daily_and_treatment_values_keeps_each_scope(django_user_model):
    from datetime import date
    from apps.self_records.services import create_record as create_daily
    from tests.self_records.test_payloads import payload as daily_payload
    from tests.exports.test_clinical_exports import _confirm
    from tests.facts.test_clinical_foundation import clinical_fixture
    from tests.labs.test_trends import _observation
    from tests.treatments.test_manual_events import create as create_treatment

    client, patient, document, _, _ = clinical_fixture(django_user_model, name='glucose-mixed-domains')
    _confirm(patient, document)
    dimension = document.facts.filter(field_key='lesion.dimensions').first()
    lab_document, lab = _observation(patient, date(2026, 8, 2), '4')
    daily = create_daily(patient, patient.account, daily_payload(), creation_key=uuid4()).record
    glucose = create_record(patient, patient.account, payload(notes='SELECTED GLUCOSE'), creation_key=uuid4()).record
    create_record(patient, patient.account, payload(notes='UNSELECTED GLUCOSE'), creation_key=uuid4())
    create_daily(patient, patient.account, daily_payload(notes='UNSELECTED DAILY'), creation_key=uuid4())
    event = create_treatment(patient, patient.account)
    scope = {**selection(glucose), 'document_ids': [str(document.pk), str(lab_document.pk)],
             'clinical_field_ids': [str(dimension.pk)], 'observation_ids': [str(lab.pk)],
             'self_record_ids': [str(daily.pk)], 'treatment_event_ids': [str(event.pk)],
             'sections': ['patient', 'glucose', 'self_records', 'imaging', 'labs', 'treatment']}
    assert client.get('/records/').status_code == 200
    job = create_preview(patient, client.session.session_key, scope, actor=patient.account)
    public = json.loads(json_bytes(job.snapshot))
    for key, expected in (('clinical_fields', dimension.pk), ('labs', lab.pk), ('self_records', daily.pk),
                          ('glucose_records', glucose.pk), ('treatment_events', event.pk)):
        assert [row['id'] for row in public[key]] == [str(expected)]
    assert 'UNSELECTED GLUCOSE' not in json.dumps(public) and 'UNSELECTED DAILY' not in json.dumps(public)
    shared = create_share(patient, patient.account, scope).share
    for key in ('clinical_fields', 'labs', 'self_records', 'glucose_records', 'treatment_events'):
        assert [row['id'] for row in shared.snapshot[key]] == [row['id'] for row in public[key]]
    assert not {'created_by', 'updated_by', 'original_data'} & set(shared.snapshot['glucose_records'][0])
    assert_snapshot_current(patient, job.snapshot)
    assert_snapshot_current(patient, shared.snapshot)
    with build_artifact(job.snapshot, {'format': 'zip', 'parts': ['json', 'csv', 'pdf']}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            assert {'csv/glucose_records.csv', 'csv/self_records.csv', 'csv/clinical_fields.csv',
                    'csv/treatment_events.csv', 'csv/labs.csv', 'visit-card.pdf'} <= set(archive.namelist())
            assert json.loads(archive.read('records.json')) == public


def test_report_glucose_share_tracks_private_document_dependency_without_granting_original_access(django_user_model):
    from django.core.exceptions import PermissionDenied
    from apps.documents.lifecycle import move_to_trash
    from apps.patients.sharing import authorize_share

    _, patient, document, _, observation = lab_source(django_user_model, marker='glucose-private-share-document')
    record = import_source(patient, observation).record
    created = create_share(patient, patient.account, selection(record))
    assert created.share.snapshot['documents'] == [] and not created.share.source_bindings.exists()
    reader, own = _patient(django_user_model, 'glucose-private-share-reader')
    assert reader.get('/shared/open/').status_code == 200
    exchange_share_token(created.token, own.account, reader.session.session_key)
    with pytest.raises(PermissionDenied):
        authorize_share(created.share.pk, own.account, reader.session.session_key, document_id=document.pk, sources=True)
    assert reader.get(f'/shared/{created.share.pk}/').status_code == 200
    move_to_trash(patient, document.pk, actor=patient.account)
    created.share.refresh_from_db()
    assert created.share.invalidated_at is not None and created.share.snapshot == {}
    assert reader.get(f'/shared/{created.share.pk}/').status_code == 410
