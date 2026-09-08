import hashlib

import pytest
from django.core.exceptions import ValidationError

from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db
URL = 'https://images.example.invalid/visit?key=SYNTHETIC_QR'
IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
POLYGON = [[.1, .2], [.3, .2], [.3, .4], [.1, .4]]


def _evidence(django_user_model, *, kind='QR'):
    from apps.cloud_imaging.models import CloudImagingEvidence, CloudImagingScan

    _, patient = _patient(django_user_model, 'cloud-evidence-' + kind)
    document, version = parsed_facts(patient, ['云影像：' + URL + '。'])
    page = document.pages.get()
    scan = CloudImagingScan.objects.create(
        patient=patient, document=document, requested_by=patient.account,
        input_fingerprint='a' * 64, rules_version='synthetic-v1', decoder_version='synthetic-v1',
    )
    fields = dict(
        document=document, document_page=page, parsing_version=version, scan=scan, kind=kind,
        payload=URL, payload_sha256=hashlib.sha256(URL.encode()).hexdigest(), payload_type='URL',
        document_sha256=document.sha256, page_width=page.width, page_height=page.height,
        source_fingerprint='b' * 64, polygon=POLYGON,
        input_sha256='c' * 64, render_profile='synthetic-1000x1400', decoder_version='synthetic-v1', transform=IDENTITY,
    )
    if kind == 'OCR':
        block = version.ocr_blocks.get()
        fields.update(ocr_block=block, start_offset=4, end_offset=4 + len(URL), polygon=block.polygon,
                      input_sha256='', render_profile='', decoder_version='', transform=[])
    if kind == 'MANUAL':
        fields.update(scan=None, polygon=None, input_sha256='', render_profile='', decoder_version='', transform=[])
    return patient, CloudImagingEvidence(**fields)


@pytest.mark.parametrize('kind', ['OCR', 'QR', 'MANUAL'])
def test_valid_source_evidence_is_append_only_and_never_renders_payload_in_repr(django_user_model, kind):
    _, evidence = _evidence(django_user_model, kind=kind)
    evidence.save()
    assert 'SYNTHETIC_QR' not in repr(evidence)
    assert 'SYNTHETIC_QR' not in str(evidence)
    evidence.payload = 'https://different.example.invalid/'
    with pytest.raises(ValidationError):
        evidence.save()
    with pytest.raises(ValidationError):
        type(evidence).objects.filter(pk=evidence.pk).update(payload='replacement')
    evidence.refresh_from_db()
    assert evidence.payload == URL


@pytest.mark.parametrize('kind', ['OCR', 'QR', 'MANUAL'])
def test_evidence_rejects_foreign_patient_or_another_document_page(django_user_model, kind):
    patient, evidence = _evidence(django_user_model, kind=kind)
    _, foreign = _patient(django_user_model, 'cloud-foreign')
    _, foreign_pages = _document(foreign, page_count=1)
    evidence.document_page = foreign_pages[0]
    with pytest.raises(ValidationError):
        evidence.save()
    _, same_patient_other_pages = _document(patient, page_count=1)
    evidence.document_page = same_patient_other_pages[0]
    with pytest.raises(ValidationError):
        evidence.save()


def test_ocr_evidence_uses_original_unicode_offsets_and_original_polygon(django_user_model):
    _, evidence = _evidence(django_user_model, kind='OCR')
    evidence.start_offset += 1
    with pytest.raises(ValidationError):
        evidence.save()
    evidence.start_offset -= 1
    evidence.polygon = POLYGON
    with pytest.raises(ValidationError):
        evidence.save()


@pytest.mark.parametrize('kind', ['QR', 'MANUAL'])
def test_non_ocr_evidence_cannot_claim_machine_text_offsets(django_user_model, kind):
    _, evidence = _evidence(django_user_model, kind=kind)
    evidence.ocr_block = evidence.parsing_version.ocr_blocks.get()
    evidence.start_offset, evidence.end_offset = 4, 4 + len(URL)
    with pytest.raises(ValidationError):
        evidence.save()


def test_qr_rejects_unproved_or_singular_geometry_and_changed_original_identity(django_user_model):
    _, evidence = _evidence(django_user_model)
    evidence.transform = [[0.0, 0.0, 0.0]] * 3
    with pytest.raises(ValidationError):
        evidence.save()
    evidence.transform = IDENTITY
    evidence.document_sha256 = 'd' * 64
    with pytest.raises(ValidationError):
        evidence.save()


def test_scan_cannot_bind_another_patients_document(django_user_model):
    from apps.cloud_imaging.models import CloudImagingScan

    patient, evidence = _evidence(django_user_model)
    _, foreign = _patient(django_user_model, 'cloud-scan-foreign')
    with pytest.raises(ValidationError):
        CloudImagingScan.objects.create(patient=foreign, document=evidence.document, requested_by=patient.account,
                                        input_fingerprint='e' * 64, rules_version='synthetic-v1', decoder_version='synthetic-v1')
