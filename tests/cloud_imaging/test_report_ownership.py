"""Report boundaries and explicitly selected output remain independent of QR identity."""
import io
import json
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from pypdf import PdfReader

from apps.cloud_imaging.readmodels import source_details
from apps.cloud_imaging.scan_services import run_scan
from apps.cloud_imaging.services import CloudConflict, revise_source
from apps.exports.content import build_snapshot
from apps.exports.formats import build_artifact, csv_tables, json_bytes
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.patients.sharing_content import project_snapshot
from tests.documents.test_detail_viewer import _patient
from tests.facts.test_clinical_foundation import clinical_fixture
from .factories import stored_document
from .test_decoding import URL
from .test_scans import queued
from .test_source_services import _decide


pytestmark = pytest.mark.django_db
FIRST = 'https://first.example.invalid/view?key=SYNTHETIC_REPORT_A'
SECOND = 'https://second.example.invalid/view?key=SYNTHETIC_REPORT_B'


def two_reports(django_user_model):
    _, patient = _patient(django_user_model, 'cloud-two-reports')
    document, page, store = stored_document(patient)
    first = ('CT诊断报告书\n检查日期：2026-08-01\n检查项目：胸部CT\n'
             '影像表现：左肺见结节，大小12×8mm。\n诊断意见：左肺结节。云影像：' + FIRST + '。\n')
    second = ('MR诊断报告书\n检查日期：2026-08-02\n检查项目：颅脑磁共振\n'
              '影像表现：右额叶见结节，大小4×3mm。\n诊断意见：右额叶结节。云影像：' + SECOND + '。')
    _, _, _, version, _ = clinical_fixture(django_user_model, document=document, texts=[first + second])
    reports = list(document.clinical_reports.order_by('ordinal'))
    assert len(reports) == 2
    for fact in document.facts.all():
        revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0,
                    expected_source=effective_fact(fact)['current_source_token'], checked_original=True)
    return patient, document, page, store, version, reports


def test_same_page_ocr_ownership_uses_its_own_span_and_shared_qr_stays_unassigned(django_user_model):
    patient, document, _, store, _, reports = two_reports(django_user_model)
    scan = queued(patient, document)
    run_scan(scan.pk, store)
    scan.refresh_from_db()
    assert scan.status == 'SUCCEEDED'
    sources = list(document.cloud_imaging_sources.select_related('evidence'))
    assert len(sources) == 3
    by_payload = {source.evidence.payload: source for source in sources}
    assert by_payload[FIRST].report_id == reports[0].pk
    assert by_payload[SECOND].report_id == reports[1].pk
    qr = by_payload[URL]
    assert qr.report_id is None and qr.evidence.kind == 'QR'
    original_evidence = qr.evidence_id
    qr = _decide(patient, qr, 'REASSIGN', changes={'report_id': str(reports[0].pk)})
    assert qr.report_id == reports[0].pk and qr.evidence_id == original_evidence
    assert source_details(patient, actor=patient.account, source_id=qr.pk)['usable']
    first = reports[0]
    revise_report(patient, actor=patient.account, report_id=first.pk, action='EXCLUDE',
                  expected_revision=first.revision_number, expected_source=report_source_token(first))
    stale = source_details(patient, actor=patient.account, source_id=qr.pk)
    assert not stale['usable'] and not stale['source_valid'] and stale['status'] == 'STALE'
    with pytest.raises(CloudConflict):
        revise_source(patient, actor=patient.account, source_id=qr.pk, action='CONFIRM', checked_original=True,
                      expected_revision=stale['revision_number'], expected_source=stale['source_token'], operation_id=uuid4())


def test_report_reassignment_rejects_another_original_even_for_the_same_owner(django_user_model):
    patient, document, _, store, _, _ = two_reports(django_user_model)
    other_document, _, _ = stored_document(patient, pdf=True)
    _, _, _, _, _ = clinical_fixture(django_user_model, document=other_document)
    foreign_report = other_document.clinical_reports.get()
    scan = queued(patient, document)
    run_scan(scan.pk, store)
    source = document.cloud_imaging_sources.get(evidence__kind='QR')
    with pytest.raises(ValidationError):
        _decide(patient, source, 'REASSIGN', changes={'report_id': str(foreign_report.pk)})
    source.refresh_from_db()
    assert source.report_id is None and source.revision_number == 1


@pytest.mark.parametrize('mode', ['report', 'field', 'field_with_excerpt'])
@pytest.mark.parametrize('consumer', ['json', 'csv', 'pdf', 'share'])
def test_fine_outputs_never_reintroduce_other_report_or_access_strings(django_user_model, mode, consumer):
    patient, document, _, store, version, reports = two_reports(django_user_model)
    first = reports[0]
    field = first.fields.get(field_key='imaging.impression')
    selection = {'document_ids': [str(document.pk)], 'details': True, 'sections': ['imaging']}
    if mode == 'report':
        selection['report_ids'] = [str(first.pk)]
    else:
        selection['clinical_field_ids'] = [str(field.pk)]
        if mode == 'field_with_excerpt':
            excerpt = document.facts.filter(representation='EXCERPT', raw_text__contains=FIRST).first()
            assert excerpt is not None
            selection['fact_ids'] = [str(excerpt.pk)]
    raw_before = list(version.ocr_blocks.values_list('text', flat=True))
    revisions_before = list(document.facts.order_by('pk').values('pk', 'raw_text', 'automatic_content', 'revision_number'))
    snapshot = build_snapshot(patient, selection)
    if consumer == 'json':
        text = json_bytes(snapshot).decode()
    elif consumer == 'csv':
        text = '\n'.join(value.decode('utf-8-sig') for value in csv_tables(snapshot).values())
    elif consumer == 'pdf':
        with build_artifact(snapshot, {'format': 'pdf'}, store) as artifact:
            text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(artifact.payload)).pages)
    else:
        text = json.dumps(project_snapshot(snapshot, selection), ensure_ascii=False)
    assert '左肺结节' in text
    assert 'SYNTHETIC_REPORT_A' not in text and 'SYNTHETIC_REPORT_B' not in text
    assert 'SYNTHETIC_QR' not in text
    if mode != 'field_with_excerpt':
        assert '右额叶' not in text and str(reports[1].pk) not in text
    assert list(version.ocr_blocks.values_list('text', flat=True)) == raw_before
    assert list(document.facts.order_by('pk').values('pk', 'raw_text', 'automatic_content', 'revision_number')) == revisions_before
