import csv
import io
import json
from uuid import uuid4

import pytest
from pypdf import PdfReader

from apps.exports.errors import ExportInputError, ExportUnavailable
from apps.exports.formats import csv_tables, json_bytes, build_artifact
from apps.exports.pdf import render_pdf
from apps.exports.services import create_preview, get_preview
from apps.patients.sharing import create_share, exchange_share_token
from apps.self_records.services import create_record, revise_record
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


def selection(record):
    return {'mode': 'documents', 'document_ids': [], 'self_record_ids': [str(record.pk)],
            'sections': ['patient', 'self_records'], 'details': True}


def test_patient_without_documents_exports_selected_record_in_json_csv_and_actual_pdf(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-empty-doc-export')
    record = create_record(patient, actor, payload(value='2', unit='lb', notes='=synthetic formula'), creation_key=uuid4()).record
    create_record(patient, actor, payload(notes='private unselected measurement'), creation_key=uuid4())
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    data = json.loads(json_bytes(job.snapshot))
    assert data['documents'] == [] and data['scope']['self_record_ids'] == [str(record.pk)]
    assert len(data['self_records']) == 1 and data['self_records'][0]['data']['raw_unit'] == 'lb'
    assert data['self_records'][0]['data']['normalized_value'] == '0.90718474'
    assert data['self_records'][0]['created_by'] == str(actor.pk)
    assert 'url' not in data['self_records'][0]['source']
    assert 'private unselected measurement' not in json.dumps(data)
    tables = csv_tables(job.snapshot)
    records = list(csv.DictReader(io.StringIO(tables['self_records.csv'].decode('utf-8-sig'))))
    assert len(records) == 1 and records[0]['id'] == str(record.pk)
    assert records[0]['raw_value'] == '2' and records[0]['raw_unit'] == 'lb'
    assert records[0]['notes'] == "'=synthetic formula"
    text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(job.snapshot))).pages)
    assert '2 lb' in text and '2026-09-08T08:25' in text and '日常记录' in text
    assert '0.90718474 kg' in text and str(actor.pk) in text.replace('\n', '') and '分钟' in text
    assert 'private unselected measurement' not in text
    assert job.self_record_sources.get().record_id == record.pk
    artifact = build_artifact(job.snapshot, {'format': 'zip', 'parts': ['json', 'csv']}, InMemoryObjectStore())
    import zipfile
    with artifact, zipfile.ZipFile(artifact.stream) as archive:
        assert any(name.endswith('self_records.csv') for name in archive.namelist())
        assert not any(name.startswith('originals/') for name in archive.namelist())


def test_empty_package_still_rejected_and_record_revision_scrubs_frozen_preview(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-export-revision')
    with pytest.raises(ExportInputError):
        create_preview(patient, client.session.session_key, {'mode': 'documents', 'document_ids': []}, actor=actor)
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='64'))
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=actor)
    revise_record(patient, actor, record.pk, action='UNDO', expected_revision=1)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}


def test_limited_record_share_without_documents_hides_other_records_and_history(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-no-document-share')
    record = create_record(patient, actor, payload(notes='old private correction'), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='63', notes='selected current note'))
    create_record(patient, actor, payload(notes='unselected private record'), creation_key=uuid4())
    created = create_share(patient, patient.account, selection(record))
    reader, own = _patient(django_user_model, 'daily-share-reader')
    assert reader.get('/shared/open/').status_code == 200
    exchange_share_token(created.token, own.account, reader.session.session_key)
    response = reader.get(f'/shared/{created.share.pk}/')
    assert response.status_code == 200 and 'selected current note' in response.content.decode()
    assert 'unselected private record' not in response.content.decode() and 'old private correction' not in response.content.decode()
    assert '/self-records/' not in response.content.decode()
    assert reader.get(f'/self-records/{record.pk}/').status_code == 404
    assert created.share.self_record_sources.get().record_id == record.pk
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=1)
    created.share.refresh_from_db()
    assert created.share.invalidated_at is not None and created.share.snapshot == {}
    assert reader.get(f'/shared/{created.share.pk}/').status_code == 410


def test_record_only_share_cannot_create_original_download_permission(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-no-original')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(ExportInputError):
        create_share(patient, patient.account, {**selection(record), 'sections': ['self_records', 'sources']}, allow_original_download=True)


def test_export_form_selects_records_without_any_document(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-http-export')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    form_page = client.get('/visit/', {'patient': str(patient.pk)})
    assert form_page.status_code == 200 and 'self_record_ids' in form_page.context['form'].fields
    response = client.post('/visit/', {**selection(record), 'patient_id': str(patient.pk), 'nickname': '合成患者',
                                       'details': 'on', 'action': 'preview'})
    assert response.status_code == 302
    from apps.exports.models import ExportJob
    job = ExportJob.objects.get(patient=patient)
    assert [row['id'] for row in job.snapshot['self_records']] == [str(record.pk)]
    preview = client.get(response.url)
    assert preview.status_code == 200 and '60.0 kg' in preview.content.decode()
    assert ('original', '单份原件') not in preview.context['form'].fields['format'].choices
    assert 'originals' not in {key for key, _ in preview.context['form'].fields['parts'].choices}


def test_share_form_accepts_record_selection_and_omits_author_account_from_projection(django_user_model):
    owner, patient, _, actor, _ = family(django_user_model, 'daily-http-share')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    response = owner.post(f'/patients/{patient.pk}/shares/', {**selection(record), 'expires_in_hours': 24})
    assert response.status_code == 201
    from apps.patients.models import PatientShare
    share = PatientShare.objects.get(patient=patient)
    row = share.snapshot['self_records'][0]
    assert row['id'] == str(record.pk) and 'created_by' not in row and 'source' not in row
    assert str(actor.pk) not in json.dumps(row)


def test_real_multichunk_download_stops_after_selected_record_correction_and_audits_once(django_user_model, monkeypatch):
    from apps.exports.services import generate_export, request_generation
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient, client, actor, _ = family(django_user_model, 'daily-stream-source-change')
    records = [create_record(patient, actor, payload(notes='合成备注' + '记' * 480), creation_key=uuid4()).record
               for _ in range(130)]
    scope = {**selection(records[0]), 'self_record_ids': [str(record.pk) for record in records]}
    job = create_preview(patient, client.session.session_key, scope, actor=actor)
    request_generation(patient, client.session.session_key, job.pk, {'format': 'json'}, actor=actor, dispatch=lambda _: None)
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
    response = client.get(f'/visit/{job.pk}/download/')
    assert response.status_code == 200 and int(response['Content-Length']) > 256 * 1024
    iterator = iter(response.streaming_content)
    assert len(next(iterator)) == 256 * 1024
    revise_record(patient, actor, records[0].pk, action='CORRECT', expected_revision=0, changes=payload(value='64'))
    assert b''.join(iterator) == b''
    response.close()
    response.close()
    events = list(AuditEvent.objects.filter(action='export_downloaded', target_hash=_hash('target', job.pk)))
    assert sorted(event.result for event in events) == ['denied', 'scheduled']
    assert len({event.request_id for event in events}) == 1
    assert all(event.actor_hash == _hash('actor', actor.pk) for event in events)
