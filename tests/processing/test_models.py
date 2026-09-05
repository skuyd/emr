import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.documents.models import Document, DocumentPage, ProcessingRun, UploadBatch
from apps.patients.models import Patient
from apps.processing.models import (
    DocumentMetadataCandidate,
    MetadataKind,
    OcrBlock,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)


pytestmark = pytest.mark.django_db(transaction=True)
POLYGON = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]]


def _document_graph(django_user_model, marker):
    account = django_user_model.objects.create(phone_hash=marker * 64, phone_encrypted="ciphertext")
    patient = Patient.objects.create(account=account, display_name="模型测试")
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="synthetic.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )
    page = DocumentPage.objects.create(
        document=document,
        page_number=1,
        width=100,
        height=200,
        orientation="PORTRAIT",
    )
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="phr-v1.0.0",
        task_type="INITIAL_PARSE",
        idempotency_key=f"{document.pk}:phr-v1.0.0:INITIAL_PARSE",
    )
    return document, page, run


def _version(document, run, **overrides):
    values = {
        "document": document,
        "processing_run": run,
        "parser_version": "phr-v1.0.0",
        "ocr_provider": "fixture",
        "ocr_provider_version": "1.0",
        "dictionary_version": "1.0.0",
        "dictionary_hash": "a" * 64,
    }
    values.update(overrides)
    return ParsingVersion.objects.create(**values)


def test_parsing_version_records_reproducible_provider_and_dictionary_identity(django_user_model):
    document, _page, run = _document_graph(django_user_model, "a")
    version = _version(document, run)

    assert version.status == ParsingVersionStatus.BUILDING
    assert version.active is False
    assert version.parser_version == "phr-v1.0.0"
    assert version.ocr_provider == "fixture"
    assert version.dictionary_hash == "a" * 64


def test_activation_locks_document_before_version_to_match_deletion(monkeypatch, django_user_model):
    from tests.documents.test_deletion import _trace_locked_rows

    document, _page, run = _document_graph(django_user_model, "l")
    version = _version(document, run, status=ParsingVersionStatus.READY)
    locks = _trace_locked_rows(monkeypatch)

    activated = ParsingVersion.objects.activate(version)

    assert activated.active is True
    assert locks[:2] == [(Document, document.pk), (ParsingVersion, version.pk)]


def test_database_allows_only_one_active_version_and_manager_switches_atomically(django_user_model):
    document, _page, first_run = _document_graph(django_user_model, "b")
    finished_at = timezone.now()
    ProcessingRun.objects.filter(pk=first_run.pk).update(
        stage="SUCCEEDED",
        finished_at=finished_at,
        is_current=True,
    )
    first = _version(
        document,
        first_run,
        status=ParsingVersionStatus.PUBLISHED,
        active=True,
        published_at=finished_at,
    )
    second_run = ProcessingRun.objects.create(
        document=document,
        parser_version="phr-v1.1.0",
        task_type="REPARSE",
        attempt_number=2,
        idempotency_key=f"{document.pk}:phr-v1.1.0:REPARSE",
    )
    ProcessingRun.objects.filter(pk=second_run.pk).update(stage="SUCCEEDED", finished_at=finished_at)
    second = _version(
        document,
        second_run,
        parser_version="phr-v1.1.0",
        status=ParsingVersionStatus.READY,
    )
    conflict_run = ProcessingRun.objects.create(
        document=document,
        parser_version="phr-conflict",
        task_type="REPARSE",
        attempt_number=3,
        idempotency_key=f"{document.pk}:phr-conflict:REPARSE",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ParsingVersion.objects.create(
            document=document,
            processing_run=conflict_run,
            parser_version="phr-conflict",
            ocr_provider="fixture",
            ocr_provider_version="1.0",
            dictionary_version="1.0.0",
            dictionary_hash="b" * 64,
            status=ParsingVersionStatus.PUBLISHED,
            active=True,
            published_at=timezone.now(),
        )

    ParsingVersion.objects.activate(second)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.active is False
    assert second.active is True
    assert second.status == ParsingVersionStatus.PUBLISHED
    assert second.published_at is not None


def test_ocr_evidence_and_metadata_retain_page_coordinates(django_user_model):
    document, page, run = _document_graph(django_user_model, "c")
    version = _version(document, run)
    block = OcrBlock.objects.create(
        parsing_version=version,
        document_page=page,
        reading_order=1,
        text="白细胞 4.2",
        polygon=POLYGON,
        confidence=0.98,
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=page,
        polygon=POLYGON,
        source_text="白细胞",
        confidence=0.98,
        ocr_block=block,
    )
    candidate = DocumentMetadataCandidate.objects.create(
        parsing_version=version,
        kind=MetadataKind.DOCUMENT_DATE,
        raw_text="2026-08",
        normalized_value="2026-08",
        precision="MONTH",
        confidence=0.91,
        evidence=evidence,
    )

    assert block.document_page.document_id == document.pk
    assert evidence.polygon == POLYGON
    assert candidate.evidence_id == evidence.pk


def test_coordinate_models_reject_out_of_range_polygons_and_cross_document_pages(django_user_model):
    document, page, run = _document_graph(django_user_model, "d")
    version = _version(document, run)
    invalid = OcrBlock(
        parsing_version=version,
        document_page=page,
        reading_order=1,
        text="invalid",
        polygon=[[0, 0], [2, 0], [2, 1]],
        confidence=0.5,
    )
    with pytest.raises(ValidationError):
        invalid.full_clean()

    _other_document, other_page, _other_run = _document_graph(django_user_model, "e")
    cross_document = SourceEvidence(
        parsing_version=version,
        document_page=other_page,
        polygon=POLYGON,
        source_text="wrong page",
        confidence=0.8,
    )
    with pytest.raises(ValidationError):
        cross_document.full_clean()
