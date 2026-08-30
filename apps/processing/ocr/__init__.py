from django.conf import settings

from .base import OcrContractError, OcrInferenceError, OcrProviderUnavailable, recognize_page
from .paddle import PaddleOcrProvider
from .text_layer import TextLayerOcrProvider


def get_raster_ocr_provider(name=None):
    name = (name or settings.PHR_OCR_PROVIDER).casefold()
    if name == "paddle":
        return PaddleOcrProvider(
            detection_model=settings.PHR_OCR_PADDLE_DETECTION_MODEL,
            recognition_model=settings.PHR_OCR_PADDLE_RECOGNITION_MODEL,
            detection_model_dir=settings.PHR_OCR_PADDLE_DETECTION_MODEL_DIR or None,
            recognition_model_dir=settings.PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR or None,
            device=settings.PHR_OCR_DEVICE,
        )
    raise OcrProviderUnavailable()


__all__ = [
    "OcrContractError",
    "OcrInferenceError",
    "OcrProviderUnavailable",
    "PaddleOcrProvider",
    "TextLayerOcrProvider",
    "get_raster_ocr_provider",
    "recognize_page",
]
