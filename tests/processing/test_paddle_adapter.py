import importlib
import sys

import pytest

from apps.processing.ocr.base import OcrContractError, OcrProviderUnavailable, recognize_page
from apps.processing.preparation import PreparedPage, PreparedPageKind


class _Result:
    def __init__(self, payload):
        self.json = {"res": payload}


class _Engine:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def predict(self, value):
        self.calls.append(value)
        return [_Result(self.payload)]


def _prepared(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(b"synthetic-png")
    return PreparedPage(1, PreparedPageKind.RASTER, 200, 100, 200, 100, raster_path=path)


def test_importing_adapter_does_not_import_optional_paddle_package(monkeypatch):
    sys.modules.pop("paddleocr", None)
    module = importlib.import_module("apps.processing.ocr.paddle")
    importlib.reload(module)
    assert "paddleocr" not in sys.modules


def test_paddle_v3_json_is_normalized_and_sorted_in_reading_order(tmp_path):
    from apps.processing.ocr.paddle import PaddleOcrProvider

    engine = _Engine(
        {
            "rec_texts": ["右侧", "左侧", "下一行"],
            "rec_scores": [0.91, 0.99, 0.95],
            "rec_polys": [
                [[120, 10], [190, 10], [190, 30], [120, 30]],
                [[10, 10], [80, 10], [80, 30], [10, 30]],
                [[10, 50], [100, 50], [100, 70], [10, 70]],
            ],
        }
    )
    provider = PaddleOcrProvider(
        engine=engine,
        package_version="3.7.0",
        detection_model="PP-OCRv5_mobile_det",
        recognition_model="PP-OCRv5_mobile_rec",
    )

    result = recognize_page(provider, _prepared(tmp_path))

    assert engine.calls and engine.calls[0].endswith("page.png")
    assert [region.text for region in result.regions] == ["左侧", "右侧", "下一行"]
    assert result.regions[0].polygon == ((0.05, 0.1), (0.4, 0.1), (0.4, 0.3), (0.05, 0.3))
    assert result.provider_version == "3.7.0"
    assert dict(result.provider_metadata) == {
        "detection_model": "PP-OCRv5_mobile_det",
        "recognition_model": "PP-OCRv5_mobile_rec",
    }


def test_adapter_initializes_offline_local_pipeline_with_preprocessing_disabled(tmp_path, monkeypatch):
    from apps.processing.ocr import paddle

    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def predict(self, _value):
            return [_Result({"rec_texts": [], "rec_scores": [], "rec_polys": []})]

    monkeypatch.setattr(paddle, "_load_paddle_ocr_class", lambda: FakePaddleOCR)
    provider = paddle.PaddleOcrProvider(
        package_version="3.7.0",
        detection_model="PP-OCRv5_mobile_det",
        recognition_model="PP-OCRv5_mobile_rec",
        detection_model_dir=tmp_path / "det",
        recognition_model_dir=tmp_path / "rec",
        device="cpu",
    )
    recognize_page(provider, _prepared(tmp_path))

    assert captured == {
        "enable_mkldnn": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_detection_model_name": "PP-OCRv5_mobile_det",
        "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        "text_detection_model_dir": str(tmp_path / "det"),
        "text_recognition_model_dir": str(tmp_path / "rec"),
        "device": "cpu",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"rec_texts": ["one"], "rec_scores": [], "rec_polys": []},
        {"rec_texts": ["one"], "rec_scores": [1.1], "rec_polys": [[[0, 0], [1, 0], [1, 1]]]},
        {"rec_texts": ["one"], "rec_scores": [0.9], "rec_polys": [[[0, 0], [300, 0], [300, 10]]]},
    ],
)
def test_malformed_provider_output_becomes_safe_contract_error(tmp_path, payload):
    from apps.processing.ocr.paddle import PaddleOcrProvider

    provider = PaddleOcrProvider(engine=_Engine(payload), package_version="3.7.0")
    with pytest.raises(OcrContractError) as error:
        recognize_page(provider, _prepared(tmp_path))
    assert str(error.value) == "invalid_ocr_result"


def test_missing_optional_package_becomes_typed_provider_unavailable(monkeypatch):
    from apps.processing.ocr import paddle

    def missing():
        raise ImportError("must not be exposed")

    monkeypatch.setattr(paddle, "_import_paddle_ocr", missing)
    with pytest.raises(OcrProviderUnavailable) as error:
        paddle._load_paddle_ocr_class()
    assert str(error.value) == "ocr_provider_unavailable"


@pytest.mark.ocr_model
@pytest.mark.skipif(
    __import__("os").environ.get("PHR_RUN_OCR_MODEL") != "1",
    reason="Set PHR_RUN_OCR_MODEL=1 in the prepared local PaddleOCR model environment",
)
def test_local_paddle_model_smoke(tmp_path):
    from PIL import Image

    from apps.processing.ocr.paddle import PaddleOcrProvider

    path = tmp_path / "smoke.png"
    Image.new("RGB", (320, 80), "white").save(path)
    prepared = PreparedPage(1, PreparedPageKind.RASTER, 320, 80, 320, 80, raster_path=path)
    result = recognize_page(PaddleOcrProvider(), prepared)
    assert result.provider == "paddleocr-local"
