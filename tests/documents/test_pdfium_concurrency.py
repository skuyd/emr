from concurrent.futures import ThreadPoolExecutor
import io
import threading

from PIL import Image
import pytest

from apps.documents.inspection import inspect_upload
from apps.documents import previews
from tests.documents.test_inspection import pdf_bytes


@pytest.mark.parametrize("second_operation", ["page", "sheet"])
def test_inspection_and_previews_serialize_complete_native_document_lifetimes(monkeypatch, second_operation):
    """A blocked native call must prevent every other Web PDF entry point."""
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    overlap = threading.Event()
    mutex = threading.Lock()
    active = 0
    opened = 0
    closed = []

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (12, 24), "white")

        def close(self):
            closed.append("bitmap")

    class Page:
        def get_size(self):
            return (72, 144)

        def render(self, **_kwargs):
            return Bitmap()

        def close(self):
            closed.append("page")

    class NativeDocument:
        def __init__(self, _path):
            nonlocal active, opened
            with mutex:
                active += 1
                opened += 1
                first = opened == 1
                if active > 1:
                    overlap.set()
            if first:
                entered.set()
                assert release.wait(5)

        def __len__(self):
            return 1

        def get_page(self, _index):
            return Page()

        __getitem__ = get_page

        def close(self):
            nonlocal active
            closed.append("document")
            with mutex:
                active -= 1

    monkeypatch.setattr("pypdfium2.PdfDocument", NativeDocument)
    payload = pdf_bytes()

    def inspect():
        with inspect_upload(io.BytesIO(payload), "synthetic.pdf") as inspected:
            return inspected.page_count

    def preview():
        second_started.set()
        if second_operation == "page":
            return previews.render_page(io.BytesIO(payload), "application/pdf", 1)
        return previews.render_thumbnail_sheet(io.BytesIO(payload), "application/pdf", 1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(inspect)
        try:
            assert entered.wait(5)
            second = executor.submit(preview)
            assert second_started.wait(5)
            overlapped = overlap.wait(0.25)
        finally:
            release.set()
        assert first.result(5) == 1
        assert second.result(5).startswith(b"\x89PNG")
    assert not overlapped
    assert active == 0
    assert closed == ["bitmap", "page", "document"] * 2


@pytest.mark.parametrize("operation", ["inspection", "page", "sheet"])
def test_pdf_render_failure_closes_native_children_before_next_call(monkeypatch, operation):
    from apps.documents.errors import InspectionError

    closed = []

    class BrokenPage:
        def get_size(self):
            return (72, 144)

        def render(self, **_kwargs):
            raise RuntimeError("synthetic native error")

        def close(self):
            closed.append("page")

    class NativeDocument:
        def __init__(self, _path):
            pass

        def __len__(self):
            return 1

        def get_page(self, _index):
            return BrokenPage()

        __getitem__ = get_page

        def close(self):
            closed.append("document")

    monkeypatch.setattr("pypdfium2.PdfDocument", NativeDocument)
    payload = pdf_bytes()
    with pytest.raises((InspectionError, previews.PreviewUnavailable)):
        if operation == "inspection":
            inspect_upload(io.BytesIO(payload), "synthetic.pdf")
        elif operation == "page":
            previews.render_page(io.BytesIO(payload), "application/pdf", 1)
        else:
            previews.render_thumbnail_sheet(io.BytesIO(payload), "application/pdf", 1)
    assert closed == ["page", "document"]


@pytest.mark.parametrize("operation", ["page", "sheet"])
def test_native_bitmap_cleanup_failure_still_closes_its_parents(monkeypatch, operation):
    closed = []

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (12, 24), "white")

        def close(self):
            closed.append("bitmap")
            raise RuntimeError("synthetic cleanup failure")

    class Page:
        def get_size(self):
            return (72, 144)

        def render(self, **_kwargs):
            return Bitmap()

        def close(self):
            closed.append("page")

    class NativeDocument:
        def __init__(self, _path):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return Page()

        def close(self):
            closed.append("document")

    monkeypatch.setattr("pypdfium2.PdfDocument", NativeDocument)
    with pytest.raises(Exception):
        if operation == "page":
            previews.render_page(io.BytesIO(pdf_bytes()), "application/pdf", 1)
        else:
            previews.render_thumbnail_sheet(io.BytesIO(pdf_bytes()), "application/pdf", 1)
    assert closed == ["bitmap", "page", "document"]
