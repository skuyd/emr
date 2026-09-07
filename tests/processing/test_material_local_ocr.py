"""Opt-in offline inference validates suggestions with the actual local model."""
import io
import hashlib
import os
from pathlib import Path
import socket

from PIL import Image, ImageDraw
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from apps.processing.material import classify_material
from apps.processing.ocr.base import recognize_page
from apps.processing.ocr.paddle import PaddleOcrProvider
from apps.processing.ocr.text_layer import TextLayerOcrProvider
from apps.processing.preparation import PreparedPageKind, prepare_document
from tests.processing.test_image_enhancement import encoded, paper_photo
from tests.processing.test_material_classification import synthetic_scene


pytestmark = [pytest.mark.ocr_model, pytest.mark.skipif(
    os.environ.get("PHR_RUN_OCR_MODEL") != "1", reason="Requires the prepared local OCR models",
)]


def test_material_suggestions_from_real_offline_ocr_do_not_discard_any_page(monkeypatch):
    attempts = []

    def offline(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("The material check must remain offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    models = Path.home() / ".paddlex/official_models"
    provider = PaddleOcrProvider(
        detection_model_dir=Path(os.environ.get("PHR_OCR_PADDLE_DETECTION_MODEL_DIR") or models / "PP-OCRv5_mobile_det"),
        recognition_model_dir=Path(os.environ.get("PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR") or models / "PP-OCRv5_mobile_rec"),
    )
    report, _ = paper_photo(perspective=False, shadow=False)
    pen_strokes = Image.new("RGB", (640, 480), "white")
    ImageDraw.Draw(pen_strokes).line([(50, 80), (95, 120), (125, 75), (160, 140), (200, 60), (240, 145)], fill=(12, 40, 100), width=4)
    cases = [
        ("synthetic-scene", synthetic_scene(), "NON_DOCUMENT"),
        ("blank", Image.new("RGB", (640, 480), "white"), "UNCERTAIN"),
        ("pen-strokes", pen_strokes, "UNCERTAIN"),
        ("report", report, "DOCUMENT"),
        ("partial-report", report.crop((20, 150, 650, 350)), "DOCUMENT"),
    ]
    for label, image, expected in cases:
        payload = encoded(image)
        with prepare_document(io.BytesIO(payload), "image/png") as prepared:
            pages = tuple(recognize_page(provider, page) for page in prepared.pages)
            result = classify_material(prepared.pages, pages)
            assert result["status"] == expected, label
            assert len(result["pages"]) == len(prepared.pages) == len(pages) == 1
            assert all(row["precision"] == "page" for row in result["pages"])

    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(640, 480))
    canvas.drawString(40, 400, "Synthetic medical record: LAB RESULT 123.45 mg/L")
    canvas.showPage()
    canvas.drawImage(ImageReader(synthetic_scene()), 0, 0, width=640, height=480)
    canvas.showPage()
    canvas.save()
    with prepare_document(io.BytesIO(output.getvalue()), "application/pdf") as prepared:
        pages = tuple(recognize_page(TextLayerOcrProvider() if page.kind == PreparedPageKind.TEXT_LAYER else provider, page) for page in prepared.pages)
        result = classify_material(prepared.pages, pages)
        assert result["status"] == "DOCUMENT"
        # PDF resampling reduces the scene's texture evidence. Keep the weaker
        # evidence uncertain rather than lowering the non-document threshold.
        assert [page["status"] for page in result["pages"]] == ["DOCUMENT", "UNCERTAIN"]
        assert len(pages) == 2
    assert attempts == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not os.environ.get("PHR_MATERIAL_PUBLIC_PHOTOS_DIR"), reason="Requires the two hash-verified public photo fixtures")
def test_public_photo_recognition_and_recovery_use_actual_offline_ocr(django_user_model, monkeypatch):
    from apps.documents.models import Document, DocumentPage, UploadBatch, UploadItem
    from apps.documents.quotas import _usage
    from apps.processing.material_review import material_state, review_material
    from apps.processing.models import ParsingVersion
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from tests.processing.test_pipeline import _document_and_run, _Store

    attempts = []

    def offline(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("The public photo OCR must remain offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    models = Path.home() / ".paddlex/official_models"
    provider = PaddleOcrProvider(
        detection_model_dir=Path(os.environ.get("PHR_OCR_PADDLE_DETECTION_MODEL_DIR") or models / "PP-OCRv5_mobile_det"),
        recognition_model_dir=Path(os.environ.get("PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR") or models / "PP-OCRv5_mobile_rec"),
    )
    fixtures = Path(os.environ["PHR_MATERIAL_PUBLIC_PHOTOS_DIR"])
    for name, digest, expected in [
        ("fruits.jpg", "9c031d80a1c52da5eca790db896baffec6a7e52bf786cdb7bbfca5c7f880e6a1", "NON_DOCUMENT"),
        ("basketball1.png", "ba06f6701f7260998b430c39b6557f775497e6ce7b1a74f0b7ea6af371bf54a6", "UNCERTAIN"),
    ]:
        path = fixtures / name
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest
        document, run = _document_and_run(django_user_model)
        Document.objects.filter(pk=document.pk).update(
            sha256=digest, byte_size=len(payload), content_type="image/jpeg" if name.endswith("jpg") else "image/png",
        )
        with Image.open(io.BytesIO(payload)) as original:
            DocumentPage.objects.filter(document=document).update(width=original.width, height=original.height)
        document.refresh_from_db()
        identity = (document.sha256, document.original_object_key, document.byte_size, document.page_count)
        counts = tuple(model.objects.count() for model in (Document, DocumentPage, UploadBatch, UploadItem))
        usage = _usage(document.patient)
        store = _Store(payload)
        pipeline = DocumentProcessingPipeline(object_store=store, raster_provider=provider)
        assert run_processing(run.pk, pipeline).state == ExecutionState.NO_STRUCTURED_RESULT
        version = ParsingVersion.objects.get(processing_run=run)
        assert version.diagnostics["material"]["status"] == expected, name
        assert version.diagnostics["material"]["pages"][0]["precision"] == "page"
        if expected == "NON_DOCUMENT":
            queued = []
            decision = review_material(
                document.patient, document.pk, actor=document.patient.account, action="KEEP_DOCUMENT",
                expected_version=str(version.pk), expected_revision=0, dispatch=queued.append,
            )
            assert queued == [decision.processing_run_id]
            assert run_processing(decision.processing_run_id, pipeline).state == ExecutionState.NO_STRUCTURED_RESULT
            document.refresh_from_db()
            active = ParsingVersion.objects.get(document=document, active=True)
            assert material_state(document)["status"] == "DOCUMENT"
            assert active.diagnostics["material"]["status"] == "NON_DOCUMENT"
            assert document.material_decisions.get().source_sha256 == digest
            review_material(
                document.patient, document.pk, actor=document.patient.account, action="AUTO",
                expected_version=str(active.pk), expected_revision=1, dispatch=queued.append,
            )
            document.refresh_from_db()
            assert material_state(document)["status"] == "NON_DOCUMENT"
            assert document.material_decisions.count() == 2
            version.refresh_from_db()
            assert not version.active and version.diagnostics["material"]["status"] == "NON_DOCUMENT"
        assert tuple(model.objects.count() for model in (Document, DocumentPage, UploadBatch, UploadItem)) == counts
        assert (document.sha256, document.original_object_key, document.byte_size, document.page_count) == identity
        assert _usage(document.patient) == usage
        assert path.read_bytes() == store.payload == payload
    assert attempts == []
