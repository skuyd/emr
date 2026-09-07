"""Opt-in real local model check. No source transcripts or external OCR are used."""
import io
import os
from pathlib import Path
import re
import socket
import unicodedata

import cv2
import numpy as np
from PIL import ImageDraw, ImageFont
import pytest

from apps.processing.geometry import source_polygon
from apps.processing.images import prepare_image
from apps.processing.ocr.base import recognize_page
from apps.processing.ocr.paddle import PaddleOcrProvider
from tests.processing.test_image_enhancement import encoded, paper_photo


pytestmark = [pytest.mark.ocr_model, pytest.mark.skipif(os.environ.get("PHR_RUN_OCR_MODEL") != "1", reason="Requires the existing local OCR models")]


@pytest.mark.parametrize("perspective,shadow", [(False, False), (True, False), (False, True), (True, True)],
                         ids=["flat", "perspective", "shadow", "combined"])
def test_local_ocr_keeps_text_and_locates_each_line_on_the_unmodified_photo(monkeypatch, perspective, shadow):
    models = Path.home()/".paddlex/official_models"
    detection = Path(os.environ.get("PHR_OCR_PADDLE_DETECTION_MODEL_DIR") or models/"PP-OCRv5_mobile_det")
    recognition = Path(os.environ.get("PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR") or models/"PP-OCRv5_mobile_rec")
    assert detection.is_dir() and recognition.is_dir(), "Provide both prepared local model directories"
    attempts = []

    def reject_network(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("The real-model enhancement check must remain offline")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    provider = PaddleOcrProvider(detection_model_dir=detection, recognition_model_dir=recognition)
    photo, forward = paper_photo(perspective=perspective, shadow=shadow)
    payload = encoded(photo)
    results = []
    for enabled in (False, True):
        with prepare_image(io.BytesIO(payload), "image/png", enhance=enabled) as prepared:
            results.append(recognize_page(provider, prepared.pages[0]))
    normalize = lambda text: re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).upper()
    line = "LABRESULT123.45MG/L"
    assert normalize(results[1].full_text).count(line) >= normalize(results[0].full_text).count(line)
    regions = [region for region in results[1].regions if line in normalize(region.text)]
    assert len(regions) == 10
    font = ImageFont.truetype("arial.ttf" if os.name == "nt" else "DejaVuSans.ttf", 26)
    for region, y in zip(regions, range(90, 650, 60), strict=True):
        x1, y1, x2, y2 = ImageDraw.Draw(photo).textbbox((40, y), "LAB RESULT 123.45 mg/L", font=font)
        expected_center = cv2.perspectiveTransform(np.float32([[[(x1+x2)/2, (y1+y2)/2]]]), forward)[0, 0]
        mapped = source_polygon(results[1].source_transform, region.polygon)
        assert mapped is not None
        source = np.float32(mapped)*np.float32([photo.width, photo.height])
        assert cv2.pointPolygonTest(source, tuple(map(float, expected_center)), False) >= 0
    assert attempts == []
