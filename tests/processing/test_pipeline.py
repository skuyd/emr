import io
import uuid

from PIL import Image
import pytest

from apps.documents.models import (
    Document,
    DocumentPage,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.labs.dictionary import default_dictionary
from apps.labs.models import LabObservation
from apps.patients.models import Patient
from apps.processing.models import DocumentSummary, DocumentType, OcrBlock, ParsingVersion, ParsingVersionStatus
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrPage, OcrRegion


pytestmark = pytest.mark.django_db(transaction=True)


class _Store:
    def __init__(self, payload):
        self.payload = payload
        self.keys = []

    def open_private(self, key):
        self.keys.append(key)
        return io.BytesIO(self.payload)


def _png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PNG")
    return output.getvalue()


def _region(text, left, right, top, order):
    return OcrRegion(
        text,
        ((left, top), (right, top), (right, top + 0.04), (left, top + 0.04)),
        0.98,
        reading_order=order,
    )


def _ocr_page(value="4.20"):
    return OcrPage(
        1,
        100,
        100,
        (
            _region("合成医学检验中心 检验报告", 0.05, 0.90, 0.04, 1),
            _region("采样日期：2026-08-20", 0.05, 0.60, 0.10, 2),
            _region("WBC 白细胞", 0.05, 0.30, 0.20, 3),
            _region(value, 0.40, 0.50, 0.20, 4),
            _region("10^9/L", 0.55, 0.68, 0.20, 5),
            _region("3.50-9.50", 0.72, 0.90, 0.20, 6),
        ),
        "fixture",
        "1.0",
    )


def _document_and_run(django_user_model):
    marker = uuid.uuid4().hex + uuid.uuid4().hex
    account = django_user_model.objects.create(phone_hash=marker, phone_encrypted="ciphertext")
    patient = Patient.objects.create(account=account, display_name="测试患者")
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
    DocumentPage.objects.create(document=document, page_number=1, width=100, height=100)
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename="synthetic.png",
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=document,
    )
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
    )
    return document, run


def _pipeline(store, page):
    return DocumentProcessingPipeline(
        object_store=store,
        raster_provider=FixtureOcrProvider((page,)),
        dictionary=default_dictionary(),
    )


def test_pipeline_persists_ocr_metadata_observation_evidence_and_activates_only_on_success(django_user_model):
    document, run = _document_and_run(django_user_model)
    store = _Store(_png_bytes())

    result = run_processing(run.pk, _pipeline(store, _ocr_page()))

    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert (version.status, version.active) == (ParsingVersionStatus.PUBLISHED, True)
    assert version.dictionary_version == default_dictionary().version
    assert version.dictionary_hash == default_dictionary().content_hash
    assert OcrBlock.objects.filter(parsing_version=version).count() == 6
    observation = LabObservation.objects.get(parsing_version=version)
    assert (observation.raw_value, observation.raw_unit, observation.reference_range_raw) == (
        "4.20",
        "10^9/L",
        "3.50-9.50",
    )
    assert observation.evidence.document_page.document_id == document.pk
    summary = DocumentSummary.objects.get(parsing_version=version)
    assert summary.document_type == DocumentType.LAB
    assert summary.document_date.isoformat() == "2026-08-20"
    assert store.keys == [document.original_object_key]


def test_reprocessing_keeps_old_version_traceable_and_atomically_switches_active_result(django_user_model):
    document, first_run = _document_and_run(django_user_model)
    store = _Store(_png_bytes())
    assert run_processing(first_run.pk, _pipeline(store, _ocr_page("4.20"))).state == ExecutionState.SUCCEEDED
    first_version = ParsingVersion.objects.get(processing_run=first_run)
    second_run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v2",
        task_type="reparse",
        idempotency_key=f"{document.pk}:parser-v2:reparse",
        attempt_number=2,
        stage=ProcessingStage.QUEUED,
    )

    result = run_processing(second_run.pk, _pipeline(store, _ocr_page("5.10")))

    assert result.state == ExecutionState.SUCCEEDED
    first_version.refresh_from_db()
    second_version = ParsingVersion.objects.get(processing_run=second_run)
    assert first_version.active is False
    assert second_version.active is True
    assert LabObservation.objects.get(parsing_version=first_version).raw_value == "4.20"
    assert LabObservation.objects.get(parsing_version=second_version).raw_value == "5.10"
    assert ParsingVersion.objects.filter(document=document).count() == 2


def test_empty_ocr_publishes_traceable_version_but_degrades_document_to_original_only(django_user_model):
    _document, run = _document_and_run(django_user_model)
    store = _Store(_png_bytes())
    empty = OcrPage(1, 100, 100, (), "fixture", "1.0")

    result = run_processing(run.pk, _pipeline(store, empty))

    assert result.state == ExecutionState.NO_STRUCTURED_RESULT
    version = ParsingVersion.objects.get(processing_run=run)
    assert (version.status, version.active) == (ParsingVersionStatus.PUBLISHED, True)
    assert version.ocr_blocks.count() == 0
    assert version.document_summary.document_type == DocumentType.UNKNOWN
