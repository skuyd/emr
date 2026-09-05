import os

from apps.processing import pipeline
from apps.labs.dictionary import default_dictionary


def test_default_pipeline_reuses_provider_but_refreshes_dictionary(monkeypatch, settings):
    providers = []
    dictionaries = []

    class Provider:
        def __init__(self, **kwargs):
            providers.append(self)
            self.config = kwargs

    def dictionary():
        dictionaries.append(1)
        return default_dictionary()

    monkeypatch.setattr(pipeline, "PaddleOcrProvider", Provider)
    monkeypatch.setattr(pipeline, "get_object_store", lambda: object())
    monkeypatch.setattr(pipeline, "current_dictionary", dictionary)
    first = pipeline.build_default_pipeline()
    second = pipeline.build_default_pipeline()

    assert first.raster_provider is second.raster_provider
    assert len(providers) == 1
    assert len(dictionaries) == 2

    settings.PHR_OCR_DEVICE = "gpu:0"
    third = pipeline.build_default_pipeline()
    assert third.raster_provider is not first.raster_provider
    parent_pid = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)
    assert pipeline.build_default_pipeline().raster_provider is not third.raster_provider


def test_pdf_preparation_respects_the_shared_native_mutex(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import io
    import threading
    import pypdfium2

    from apps.core.pdfium import PDFIUM_LOCK
    from apps.processing.pdf import prepare_pdf
    from tests.processing.test_pdf_preparation import _pdf_bytes

    entered = threading.Event()
    started = threading.Event()
    original = pypdfium2.PdfDocument

    def document(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(pypdfium2, "PdfDocument", document)
    payload = _pdf_bytes(["Synthetic text layer with 1234567890"])

    def prepare():
        started.set()
        with prepare_pdf(io.BytesIO(payload)) as prepared:
            return len(prepared.pages)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with PDFIUM_LOCK:
            future = executor.submit(prepare)
            assert started.wait(3)
            overlapped = entered.wait(0.25)
        assert future.result(5) == 1
    assert not overlapped
