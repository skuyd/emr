import csv
import io
import json
import zipfile
from uuid import uuid4

import pytest

from apps.cloud_imaging.scan_services import run_scan
from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.patients.sharing import create_share
from .factories import stored_document
from .test_controlled_open import confirmed
from .test_report_ownership import FIRST, SECOND, two_reports
from .test_scans import queued
from .test_selected_output import source_selection
from .test_source_services import _decide

pytestmark = pytest.mark.django_db


def test_mixed_document_a_and_source_b_zip_preserves_only_a_original(django_user_model):
    _, patient, document_b, source = confirmed(django_user_model)
    document_a, _, store = stored_document(patient, pdf=True)
    expected = store.objects[document_a.original_object_key]
    selection = source_selection(source, document_ids=[str(document_a.pk)], sections=['sources'])
    snapshot = build_snapshot(patient, selection)
    artifact = build_artifact(snapshot, {'format':'zip', 'parts':['json','originals']}, store)
    try:
        with zipfile.ZipFile(artifact.stream) as bundle:
            originals = [name for name in bundle.namelist() if name.startswith('originals/')]
            assert len(originals) == 1 and bundle.read(originals[0]) == expected
            data = json.loads(bundle.read('records.json'))
            assert [row['id'] for row in data['documents']] == [str(document_a.pk)]
            assert data['cloud_imaging_sources'][0]['document_id'] == str(document_b.pk)
    finally:
        artifact.close()
    share = create_share(patient, patient.account, selection).share
    assert list(share.source_bindings.values_list('document_id', flat=True)) == [document_a.pk]
    assert share.cloud_sources.get().document_id == document_b.pk


@pytest.mark.parametrize('fine', ['report','field','intersection','empty_report','empty_field','unassigned','mixed_foreign'])
def test_all_selected_sources_obey_exact_fine_scope_without_silent_filtering(django_user_model, fine):
    patient, document, _, store, _, reports = two_reports(django_user_model)
    run_scan(queued(patient, document).pk, store)
    a = _decide(patient, document.cloud_imaging_sources.get(evidence__payload=FIRST), 'CONFIRM')
    b = _decide(patient, document.cloud_imaging_sources.get(evidence__payload=SECOND), 'CONFIRM')
    q = _decide(patient, document.cloud_imaging_sources.get(evidence__kind='QR'), 'CONFIRM')
    field_a = str(reports[0].fields.get(field_key='imaging.impression').pk)
    field_b = str(reports[1].fields.get(field_key='imaging.impression').pk)
    selection = source_selection(a, document_ids=[str(document.pk)])
    if fine == 'report': selection['report_ids'] = [str(reports[0].pk)]
    elif fine == 'field': selection['clinical_field_ids'] = [field_a]
    elif fine == 'intersection': selection.update(report_ids=[str(reports[0].pk)], clinical_field_ids=[field_b])
    elif fine == 'empty_report': selection['report_ids'] = []
    elif fine == 'empty_field': selection['clinical_field_ids'] = []
    elif fine == 'unassigned': selection.update(cloud_source_ids=[str(q.pk)], report_ids=[str(reports[0].pk)])
    else: selection['cloud_source_ids'].append(str(uuid4()))
    if fine not in {'report','field'}:
        with pytest.raises(ExportInputError): build_snapshot(patient, selection)
        return
    result = read_structured_data(json_bytes(build_snapshot(patient, selection)))
    assert [row['id'] for row in result['cloud_imaging_sources']] == [str(a.pk)]
    assert SECOND not in json.dumps(result)
    selection['cloud_source_ids'].append(str(b.pk))
    with pytest.raises(ExportInputError): build_snapshot(patient, selection)


def test_ocr_qr_output_keeps_original_provenance_and_only_selected_current_url(django_user_model):
    patient, document, _, store, _, _ = two_reports(django_user_model)
    run_scan(queued(patient, document).pk, store)
    sources = [_decide(patient, row, 'CONFIRM') for row in document.cloud_imaging_sources.order_by('pk')]
    raw = list(document.cloud_imaging_evidence.order_by('pk').values())
    selection = source_selection(sources[0], cloud_source_ids=[str(row.pk) for row in sources])
    data = read_structured_data(json_bytes(build_snapshot(patient, selection)))
    assert len(data['cloud_imaging_sources']) == 3
    for source in sources:
        evidence = next(row for row in data['cloud_imaging_evidence'] if row['source_id'] == str(source.pk))
        original = source.evidence
        assert evidence['polygon'] == original.polygon and evidence['transform'] == original.transform
        assert evidence['start_offset'] == original.start_offset and evidence['end_offset'] == original.end_offset
        assert evidence['decoder_version'] == original.decoder_version
        assert 'payload' not in evidence and 'raw_text' not in evidence
    assert list(document.cloud_imaging_evidence.order_by('pk').values()) == raw


def test_encoded_selected_target_preserved_other_urls_omitted_and_csv_formula_guarded(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    url = 'https://images.example.invalid/影像/a%2fb?next=https://other.invalid/&x=1&x=SECOND%2Bv#access-token'
    source = _decide(patient, source, 'CORRECT', changes={'url':url, 'title':'=1+2'})
    snapshot = build_snapshot(patient, source_selection(source, basic_info='https://unselected.example.invalid/private'))
    data = read_structured_data(json_bytes(snapshot))
    assert data['cloud_imaging_sources'][0]['current_url'] == url
    assert 'unselected.example.invalid' not in json.dumps(data)
    rows = list(csv.DictReader(io.StringIO(csv_tables(snapshot)['cloud_imaging_sources.csv'].decode('utf-8-sig'))))
    assert rows[0]['current_url'] == url and rows[0]['title'] == "'=1+2"


def test_unselected_source_change_does_not_invalidate_selected_output(django_user_model):
    from apps.exports.content import assert_snapshot_current
    from apps.cloud_imaging.readmodels import document_snapshot
    from apps.cloud_imaging.services import add_manual_source
    _, patient, document, source = confirmed(django_user_model)
    current = document_snapshot(patient, actor=patient.account, document_id=document.pk)
    other = add_manual_source(patient, actor=patient.account, document_id=document.pk,
        page_id=document.pages.get().pk, url='https://other.example.invalid/view', title='',
        expected_source=current['input_token'], operation_id=uuid4())
    snapshot = build_snapshot(patient, source_selection(source))
    _decide(patient, other, 'CONFIRM')
    assert_snapshot_current(patient, snapshot)
