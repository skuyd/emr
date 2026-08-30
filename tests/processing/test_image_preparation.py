import hashlib
import io

from PIL import Image
import pillow_heif
import pytest

from apps.processing.images import prepare_image
from apps.processing.preparation import PreparationError, PreparedPageKind, prepare_document


def _image_bytes(format_name, *, size=(40, 20), orientation=None):
    image = Image.new("RGB", size, "#64748b")
    output = io.BytesIO()
    if format_name == "HEIF":
        pillow_heif.from_pillow(image).save(output)
    else:
        options = {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        image.save(output, format=format_name, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    ("content_type", "format_name"),
    [("image/jpeg", "JPEG"), ("image/png", "PNG"), ("image/heic", "HEIF")],
)
def test_supported_images_produce_private_png_ocr_page_without_changing_original(content_type, format_name):
    payload = _image_bytes(format_name)
    original_hash = hashlib.sha256(payload).hexdigest()

    with prepare_image(io.BytesIO(payload), content_type) as prepared:
        page = prepared.pages[0]
        assert page.kind == PreparedPageKind.RASTER
        assert (page.page_number, page.width, page.height) == (1, 40, 20)
        assert page.raster_path.is_file()
        assert page.open_raster().read(8) == b"\x89PNG\r\n\x1a\n"
    assert hashlib.sha256(payload).hexdigest() == original_hash


def test_exif_rotation_is_applied_only_to_derived_page_and_recorded():
    payload = _image_bytes("JPEG", size=(40, 20), orientation=6)
    original_hash = hashlib.sha256(payload).hexdigest()

    with prepare_image(io.BytesIO(payload), "image/jpeg") as prepared:
        page = prepared.pages[0]
        assert (page.source_width, page.source_height) == (20, 40)
        assert (page.width, page.height) == (20, 40)
        assert page.source_rotation == 90
        assert "orientation_applied" in prepared.warnings
    assert hashlib.sha256(payload).hexdigest() == original_hash


def test_large_image_is_downscaled_for_ocr_but_retains_oriented_source_dimensions(monkeypatch):
    payload = _image_bytes("PNG", size=(100, 50))
    monkeypatch.setattr("apps.processing.images.MAX_OCR_PAGE_PIXELS", 1_000)

    with prepare_image(io.BytesIO(payload), "image/png") as prepared:
        page = prepared.pages[0]
        assert (page.source_width, page.source_height) == (100, 50)
        assert page.width * page.height <= 1_000
        assert page.width / page.height == pytest.approx(2.0, rel=0.05)
        assert "ocr_image_downscaled" in prepared.warnings


def test_router_rejects_unsupported_or_unreadable_image_with_stable_code():
    with pytest.raises(PreparationError) as unsupported:
        prepare_document(io.BytesIO(b"data"), "text/plain")
    assert unsupported.value.code == "unsupported_file"

    with pytest.raises(PreparationError) as unreadable:
        prepare_document(io.BytesIO(b"not-a-png"), "image/png")
    assert unreadable.value.code == "unreadable_file"
