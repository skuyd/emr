import uuid

from django.core.exceptions import ValidationError
from django.utils import timezone
import pytest

from apps.documents.models import Document, DocumentPage, ProcessingRun, UploadBatch
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.patients.models import Patient
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    ParsingVersion,
    SourceEvidence,
)


pytestmark = pytest.mark.django_db


def _document(django_user_model, marker):
    account = django_user_model.objects.create(phone_hash=marker * 64, phone_encrypted="ciphertext")
    patient = Patient.objects.create(account=account, display_name="测试患者")
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="synthetic.pdf",
        content_type="application/pdf",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )
    page = DocumentPage.objects.create(document=document, page_number=1, width=1000, height=1400)
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
    )
    version = ParsingVersion.objects.create(
        document=document,
        processing_run=run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        dictionary_version="1.0.0",
        dictionary_hash="a" * 64,
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=page,
        polygon=((0.1, 0.2), (0.8, 0.2), (0.8, 0.3), (0.1, 0.3)),
        source_text="合成检验字段",
        confidence="0.9900",
    )
    return document, page, version, evidence


def test_observation_preserves_raw_value_unit_status_and_dictionary_lineage(django_user_model):
    _document_value, page, version, evidence = _document(django_user_model, "a")
    observation = LabObservation(
        parsing_version=version,
        document_page=page,
        evidence=evidence,
        reading_order=1,
        raw_name="乙型肝炎表面抗原",
        standard_code="LAB_HBSAG",
        standard_name="乙型肝炎表面抗原",
        raw_value="未检出",
        result_type=ResultType.STATUS,
        raw_unit="",
        reference_range_raw="阴性",
        report_flag_raw="",
        capability_level=CapabilityLevel.EXPLORATORY,
        dictionary_version="1.0.0",
    )

    observation.full_clean()
    observation.save()
    loaded = LabObservation.objects.get(pk=observation.pk)

    assert loaded.raw_value == "未检出"
    assert loaded.raw_value != "0"
    assert loaded.raw_unit == ""
    assert loaded.dictionary_version == version.dictionary_version
    assert loaded.evidence.polygon[0] == [0.1, 0.2]


def test_observation_evidence_page_and_dictionary_must_match_parsing_version(django_user_model):
    _first_document, _first_page, first_version, _first_evidence = _document(django_user_model, "b")
    _second_document, second_page, _second_version, second_evidence = _document(django_user_model, "c")
    observation = LabObservation(
        parsing_version=first_version,
        document_page=second_page,
        evidence=second_evidence,
        reading_order=1,
        raw_name="白细胞",
        standard_code="LAB_WBC",
        standard_name="白细胞计数",
        raw_value="4.20",
        result_type=ResultType.NUMERIC,
        raw_unit="10^9/L",
        capability_level=CapabilityLevel.STABLE,
        dictionary_version="other-version",
    )

    with pytest.raises(ValidationError) as error:
        observation.full_clean()

    assert {"document_page", "evidence", "dictionary_version"} <= set(error.value.message_dict)


def test_document_summary_keeps_unknown_dates_unknown_and_validates_precision(django_user_model):
    document, _page, version, _evidence = _document(django_user_model, "d")
    summary = DocumentSummary(
        parsing_version=version,
        document_type=DocumentType.LAB,
        document_date_raw="",
        document_date=None,
        date_precision=DatePrecision.UNKNOWN,
        institution_raw="合成检验机构",
        confidence="0.9000",
    )
    summary.full_clean()
    summary.save()

    assert summary.parsing_version.document_id == document.pk
    summary.document_date = timezone.localdate()
    with pytest.raises(ValidationError) as error:
        summary.full_clean()
    assert "date_precision" in error.value.message_dict
