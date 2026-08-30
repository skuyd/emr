from importlib.metadata import PackageNotFoundError, version
import math
from numbers import Real
import os

from apps.processing.preparation import PreparedPageKind
from apps.processing.value_objects import InvalidRegion, OcrPage, OcrRegion

from .base import OcrContractError, OcrInferenceError, OcrProviderUnavailable


DEFAULT_DETECTION_MODEL = "PP-OCRv5_mobile_det"
DEFAULT_RECOGNITION_MODEL = "PP-OCRv5_mobile_rec"


def _import_paddle_ocr():
    from paddleocr import PaddleOCR

    return PaddleOCR


def _load_paddle_ocr_class():
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    try:
        return _import_paddle_ocr()
    except ImportError:
        raise OcrProviderUnavailable() from None


def _installed_version():
    try:
        return version("paddleocr")
    except PackageNotFoundError:
        raise OcrProviderUnavailable() from None


def _result_payload(result):
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if not isinstance(payload, dict):
        raise OcrContractError()
    payload = payload.get("res", payload)
    if not isinstance(payload, dict):
        raise OcrContractError()
    return payload


def _normalized_polygon(value, width, height):
    try:
        points = tuple(tuple(point) for point in value)
    except (TypeError, ValueError):
        raise OcrContractError() from None
    if not 3 <= len(points) <= 16:
        raise OcrContractError()
    normalized = []
    for point in points:
        if len(point) != 2:
            raise OcrContractError()
        x, y = point
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, Real)
            or not isinstance(y, Real)
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
            or float(x) < -1
            or float(y) < -1
            or float(x) > width + 1
            or float(y) > height + 1
        ):
            raise OcrContractError()
        normalized.append((max(0.0, min(1.0, float(x) / width)), max(0.0, min(1.0, float(y) / height))))
    return tuple(normalized)


def _confidence(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise OcrContractError()
    value = float(value)
    if not 0 <= value <= 1:
        raise OcrContractError()
    return value


class PaddleOcrProvider:
    def __init__(
        self,
        *,
        engine=None,
        package_version=None,
        detection_model=DEFAULT_DETECTION_MODEL,
        recognition_model=DEFAULT_RECOGNITION_MODEL,
        detection_model_dir=None,
        recognition_model_dir=None,
        device="cpu",
    ):
        self._engine = engine
        self.package_version = package_version
        self.detection_model = detection_model
        self.recognition_model = recognition_model
        self.detection_model_dir = detection_model_dir
        self.recognition_model_dir = recognition_model_dir
        self.device = device

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        paddle_ocr = _load_paddle_ocr_class()
        parameters = {
            "enable_mkldnn": False,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_detection_model_name": self.detection_model,
            "text_recognition_model_name": self.recognition_model,
            "device": self.device,
        }
        if self.detection_model_dir is not None:
            parameters["text_detection_model_dir"] = str(self.detection_model_dir)
        if self.recognition_model_dir is not None:
            parameters["text_recognition_model_dir"] = str(self.recognition_model_dir)
        try:
            self._engine = paddle_ocr(**parameters)
        except Exception:
            raise OcrProviderUnavailable() from None
        return self._engine

    def recognize(self, page):
        if page.kind != PreparedPageKind.RASTER or page.raster_path is None:
            raise OcrContractError()
        package_version = self.package_version or _installed_version()
        engine = self._get_engine()
        try:
            results = list(engine.predict(str(page.raster_path)))
        except (OcrContractError, OcrProviderUnavailable):
            raise
        except Exception:
            raise OcrInferenceError() from None
        if len(results) != 1:
            raise OcrContractError()
        payload = _result_payload(results[0])
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        polygons = payload.get("rec_polys")
        try:
            texts, scores, polygons = tuple(texts), tuple(scores), tuple(polygons)
        except TypeError:
            raise OcrContractError() from None
        if not (len(texts) == len(scores) == len(polygons)):
            raise OcrContractError()
        candidates = []
        for original_order, (text, score, polygon) in enumerate(zip(texts, scores, polygons, strict=True)):
            if not isinstance(text, str):
                raise OcrContractError()
            text = text.strip()
            normalized = _normalized_polygon(polygon, page.width, page.height)
            confidence = _confidence(score)
            if not text:
                continue
            sort_key = (
                min(point[1] for point in normalized),
                min(point[0] for point in normalized),
                original_order,
            )
            candidates.append((sort_key, text, normalized, confidence))
        candidates.sort(key=lambda item: item[0])
        try:
            regions = tuple(
                OcrRegion(text, polygon, confidence, reading_order=index)
                for index, (_sort, text, polygon, confidence) in enumerate(candidates, start=1)
            )
        except InvalidRegion:
            raise OcrContractError() from None
        return OcrPage(
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            regions=regions,
            provider="paddleocr-local",
            provider_version=package_version,
            provider_metadata={
                "detection_model": self.detection_model,
                "recognition_model": self.recognition_model,
            },
        )
