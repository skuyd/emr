import json
from uuid import uuid4

import pytest

from apps.exports.formats import json_bytes, read_structured_data
from apps.exports.models import ExportJob
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientShare
from apps.self_records.services import create_record, revise_record
from tests.exports.test_clinical_exports import _confirm
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.self_records.test_export_integration import selection
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('scope_type', ['record_only', 'mixed_change_record', 'mixed_change_field'])
def test_actual_forms_keep_clinical_fields_and_daily_records_independently_selected(django_user_model, scope_type):
    client, patient, document, _, _ = clinical_fixture(django_user_model, name='daily-clinical-' + scope_type)
    _confirm(patient, document)
    field = document.facts.get(field_key='imaging.impression')
    record = create_record(patient, patient.account, payload(notes='本次选定日常内容'), creation_key=uuid4()).record
    create_record(patient, patient.account, payload(notes='未选择的日常内容'), creation_key=uuid4())
    mixed = scope_type != 'record_only'
    scope = selection(record)
    if mixed:
        scope.update(document_ids=[str(document.pk)], clinical_field_ids=[str(field.pk)], sections=['patient', 'imaging', 'self_records'])
    assert client.get('/visit/').status_code == 200
    response = client.post('/visit/', {**scope, 'patient_id': str(patient.pk), 'nickname': '合成患者',
                                       'custom_clinical_fields': 'on' if mixed else '', 'details': 'on', 'action': 'preview'})
    assert response.status_code == 302, (response.context['form'].errors, response.context['error'])
    job = ExportJob.objects.get(patient=patient)
    data = json.loads(json_bytes(job.snapshot))
    assert [row['id'] for row in data['self_records']] == [str(record.pk)]
    assert [row['id'] for row in data['clinical_fields']] == ([str(field.pk)] if mixed else [])
    assert data['facts'] == data['labs'] == []
    assert '未选择的日常内容' not in json.dumps(data, ensure_ascii=False)
    if not mixed:
        assert data['documents'] == data['clinical_reports'] == data['clinical_field_sources'] == []
        assert field.automatic_content['text'] not in json.dumps(data, ensure_ascii=False)
    shared = client.post(f'/patients/{patient.pk}/shares/', {**scope, 'expires_in_hours': 24})
    assert shared.status_code == 201
    share = PatientShare.objects.get(patient=patient)
    assert [row['id'] for row in share.snapshot['self_records']] == [str(record.pk)]
    assert [row['id'] for row in share.snapshot['clinical_fields']] == ([str(field.pk)] if mixed else [])
    assert share.snapshot['facts'] == share.snapshot['labs'] == []
    assert '未选择的日常内容' not in json.dumps(share.snapshot, ensure_ascii=False)
    assert not share.allow_original_download
    if scope_type == 'mixed_change_field':
        revise_fact(patient, field.pk, actor=patient.account, action='REVOKE', expected_revision=1,
                    expected_source=effective_fact(field)['current_source_token'])
        # Clinical source changes are detected by the same live validation used
        # by downloads and shared responses, including both kinds of content.
        from apps.exports.errors import ExportUnavailable
        from apps.exports.services import get_preview
        from apps.patients.sharing import validate_managed_share
        with pytest.raises(ExportUnavailable):
            get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
        validate_managed_share(patient, patient.account, share.pk)
    else:
        revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
    job.refresh_from_db()
    share.refresh_from_db()
    assert job.snapshot == {} and job.status == 'INVALIDATED'
    assert share.snapshot == {} and share.invalidated_at is not None


@pytest.mark.parametrize('version', ['1.0', '1.1', '1.2'])
def test_portable_reader_accepts_prior_formats_without_rewriting_their_bytes(version):
    data = {'schema_version': version, 'documents': [], 'facts': [], 'labs': [], 'sources': []}
    if version != '1.0':
        data.update(clinical_reports=[], clinical_fields=[], clinical_field_sources=[])
    if version == '1.2':
        data['self_records'] = []
    encoded = json.dumps(data).encode()
    original = encoded[:]
    restored = read_structured_data(encoded)
    assert encoded == original and restored['schema_version'] == version
    assert restored['self_records'] == restored['clinical_fields'] == []


def test_new_format_requires_its_daily_record_table():
    from apps.exports.errors import ExportInputError
    payload = {'schema_version': '1.2', 'documents': [], 'facts': [], 'labs': [], 'sources': [],
               'clinical_reports': [], 'clinical_fields': [], 'clinical_field_sources': []}
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(payload))
