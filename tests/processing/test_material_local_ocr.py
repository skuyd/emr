"""Opt-in offline inference validates suggestions with the actual local model."""
import io
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
