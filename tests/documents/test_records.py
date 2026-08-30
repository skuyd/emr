from datetime import date, timedelta
import uuid

from django.test import Client
from django.utils import timezone
import pytest

from apps.documents.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
)
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.patients.services import create_patient_space
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    OcrBlock,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)


pytestmark = pytest.mark.django_db
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "records-test"}


def _patient(django_user_model, marker):
    account = django_user_model.objects.create(phone_hash=marker * 64, phone_encrypted="ciphertext")
    patient = create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client = Client()
    client.force_login(account)
    return client, patient


def _record(
    patient,
    name,
    *,
    document_date=None,
    precision=DatePrecision.UNKNOWN,
    document_type=DocumentType.OTHER,
    institution="",
    status=DocumentStatus.ORGANIZED,
    ocr_text="",
    observation=None,
    uploaded_at=None,
):
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename=name,
        content_type="application/pdf",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
        status=status,
    )
    if uploaded_at is not None:
        Document.objects.filter(pk=document.pk).update(created_at=uploaded_at)
        document.refresh_from_db()
    page = DocumentPage.objects.create(document=document, page_number=1, width=1000, height=1400)
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
        stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(),
        is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=document,
        processing_run=run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        dictionary_version="1.0.0",
        dictionary_hash="a" * 64,
        status=ParsingVersionStatus.READY,
    )
    ParsingVersion.objects.activate(version)
    DocumentSummary.objects.create(
        parsing_version=version,
        document_type=document_type,
        document_date_raw=document_date.isoformat() if document_date else "",
        document_date=document_date,
        date_precision=precision,
        institution_raw=institution,
        confidence="0.9000",
    )
    if ocr_text:
        OcrBlock.objects.create(
            parsing_version=version,
            document_page=page,
            reading_order=1,
            text=ocr_text,
            polygon=((0.1, 0.1), (0.8, 0.1), (0.8, 0.2), (0.1, 0.2)),
            confidence="0.9800",
        )
    if observation:
        evidence = SourceEvidence.objects.create(
            parsing_version=version,
            document_page=page,
            polygon=((0.1, 0.3), (0.8, 0.3), (0.8, 0.4), (0.1, 0.4)),
            source_text="synthetic evidence",
            confidence="0.9800",
        )
        LabObservation.objects.create(
            parsing_version=version,
            document_page=page,
            evidence=evidence,
            reading_order=1,
            raw_name=observation[0],
            standard_code="LAB_WBC",
            standard_name=observation[1],
            raw_value=observation[2],
            result_type=ResultType.NUMERIC,
            raw_unit=observation[3],
            capability_level=CapabilityLevel.STABLE,
            dictionary_version="1.0.0",
        )
    return document


def test_records_sort_recognized_dates_descending_then_unknown_by_upload_time(django_user_model):
    client, patient = _patient(django_user_model, "a")
    now = timezone.now()
    unknown = _record(patient, "unknown.pdf", uploaded_at=now)
    older = _record(
        patient,
        "older.pdf",
        document_date=date(2026, 7, 1),
        precision=DatePrecision.DAY,
        uploaded_at=now - timedelta(days=1),
    )
    newer = _record(
        patient,
        "newer.pdf",
        document_date=date(2026, 8, 1),
        precision=DatePrecision.DAY,
        uploaded_at=now - timedelta(days=2),
    )

    response = client.get("/records/")
    content = response.content.decode()

    assert response.status_code == 200
    assert content.index(newer.display_filename) < content.index(older.display_filename) < content.index(unknown.display_filename)
    assert "日期未识别" in content
    assert "2026年8月1日" in content


@pytest.mark.parametrize("query", ["OCR关键字", "原始白细胞", "白细胞计数", "4.20", "10^9/L", "合成检验中心"])
def test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet(django_user_model, query):
    client, patient = _patient(django_user_model, uuid.uuid4().hex[0])
    matched = _record(
        patient,
        "matched.pdf",
        document_date=date(2026, 8, 20),
        precision=DatePrecision.DAY,
        document_type=DocumentType.LAB,
        institution="合成检验中心",
        ocr_text="这里包含OCR关键字和原始报告片段",
        observation=("原始白细胞", "白细胞计数", "4.20", "10^9/L"),
    )
    _record(patient, "not-matched.pdf", ocr_text="完全不同的内容")

    response = client.get("/records/", {"q": query})
    content = response.content.decode()

    assert response.status_code == 200
    assert matched.display_filename in content
    assert "not-matched.pdf" not in content
    assert query.casefold() in content.casefold()
    assert "医学结论" not in content


def test_type_and_status_filters_stack_and_other_tenant_never_leaks(django_user_model):
    client, patient = _patient(django_user_model, "b")
    _other_client, other = _patient(django_user_model, "c")
    expected = _record(
        patient,
        "expected.pdf",
        document_type=DocumentType.LAB,
        status=DocumentStatus.ORIGINAL_ONLY,
    )
    _record(patient, "wrong-type.pdf", document_type=DocumentType.IMAGING, status=DocumentStatus.ORIGINAL_ONLY)
    _record(patient, "wrong-status.pdf", document_type=DocumentType.LAB, status=DocumentStatus.ORGANIZED)
    _record(other, "other-tenant-secret.pdf", document_type=DocumentType.LAB, status=DocumentStatus.ORIGINAL_ONLY)

    response = client.get("/records/", {"type": "LAB", "status": "ORIGINAL_ONLY"})
    content = response.content.decode()

    assert expected.display_filename in content
    assert "wrong-type.pdf" not in content
    assert "wrong-status.pdf" not in content
    assert "other-tenant-secret.pdf" not in content


def test_records_empty_state_and_deleted_documents_are_hidden(django_user_model):
    client, patient = _patient(django_user_model, "d")
    deleted = _record(patient, "deleted-secret.pdf")
    Document.objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())

    response = client.get("/records/", {"q": "nothing"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "没有找到相关资料。可以换个关键词，或直接按日期浏览。" in content
    assert "deleted-secret.pdf" not in content


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("2026年8月20日", "dated.pdf"),
        ("2026-08-20", "dated.pdf"),
        ("检验报告", "dated.pdf"),
        ("已整理", "dated.pdf"),
    ],
)
def test_search_understands_normalized_dates_type_and_status_labels(django_user_model, query, expected):
    client, patient = _patient(django_user_model, "e")
    _record(
        patient,
        expected,
        document_date=date(2026, 8, 20),
        precision=DatePrecision.DAY,
        document_type=DocumentType.LAB,
    )
    _record(patient, "other.pdf", document_type=DocumentType.IMAGING, status=DocumentStatus.PROCESSING_FAILED)

    content = client.get("/records/", {"q": query}).content.decode()

    assert expected in content
    assert "other.pdf" not in content
    assert query in content


def test_records_paginate_twenty_at_a_time_and_preserve_active_filters(django_user_model):
    client, patient = _patient(django_user_model, "f")
    for index in range(21):
        _record(patient, f"record-{index:02}.pdf", document_type=DocumentType.LAB)

    first = client.get("/records/", {"q": "record", "type": "LAB"}).content.decode()
    second = client.get("/records/", {"q": "record", "type": "LAB", "page": 2}).content.decode()

    assert first.count('class="record-card"') == 20
    assert "第 1 / 2 页" in first
    assert "q=record&amp;type=LAB" in first
    assert second.count('class="record-card"') == 1
    assert "第 2 / 2 页" in second
