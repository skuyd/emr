import hashlib
from importlib import metadata
import io
from pathlib import Path
import struct
import tempfile
import zlib

from PIL import Image
import pillow_heif
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject

from apps.documents.errors import ArtifactClosed, InspectionError
from apps.documents import inspection
from apps.documents.inspection import inspect_upload


pillow_heif.register_heif_opener()


class NonSeekable(io.BytesIO):
    def seekable(self):
        return False

    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("not seekable")


class EndlessJpeg:
    def __init__(self):
        self.total_read = 0
        self.first = True

    def read(self, size):
        self.total_read += size
        if self.first:
            self.first = False
            return b"\xff\xd8\xff" + b"\x00" * (size - 3)
        return b"\x00" * size


class BrokenStream:
    def read(self, _size):
        raise RuntimeError("decoder secret")


def image_bytes(image_format, *, size=(12, 8), save_all=False):
    output = io.BytesIO()
    first = Image.new("RGB", size, "#7a8ea3")
    parameters = {}
    if save_all:
        parameters = {"save_all": True, "append_images": [Image.new("RGB", size, "#ffffff")]}
    first.save(output, format=image_format, **parameters)
    return output.getvalue()


def pdf_bytes(*, pages=1, encrypted=False, javascript=False, attachment=False, form_action=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=144)
    if encrypted:
        writer.encrypt("password")
    if javascript:
        writer.add_js("app.alert('unsafe')")
    if attachment:
        writer.add_attachment("payload.txt", b"not medical data")
    if form_action:
        field = DictionaryObject({NameObject("/FT"): NameObject("/Tx"), NameObject("/AA"): DictionaryObject()})
        form = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def oversized_png_header(width=100_000, height=100_000):
    payload = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = b"IHDR" + payload
    iend = b"IEND"
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(payload))
        + ihdr
        + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + iend
        + struct.pack(">I", zlib.crc32(iend) & 0xFFFFFFFF)
    )


@pytest.mark.parametrize(
    ("image_format", "filename", "content_type", "extension"),
    [
        ("JPEG", "scan.JPEG", "image/jpeg", "jpg"),
        ("PNG", "scan.png", "image/png", "png"),
        ("HEIF", "scan.heic", "image/heic", "heic"),
    ],
)
def test_real_images_are_inspected_from_nonseekable_exact_bytes(image_format, filename, content_type, extension):
    payload = image_bytes(image_format)
    source = NonSeekable(payload)

    with inspect_upload(source, filename) as artifact:
        assert artifact.content_type == content_type
        assert artifact.extension == extension
        assert artifact.byte_size == len(payload)
        assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
        assert artifact.page_count == 1
        assert artifact.dimensions == ((12, 8),)
        with artifact.open() as replay:
            assert replay.read() == payload

    assert artifact.closed is True
    with pytest.raises(ArtifactClosed):
        artifact.open()


def test_real_pdf_is_structurally_parsed_and_every_page_render_validated():
    payload = pdf_bytes(pages=2)

    with inspect_upload(NonSeekable(payload), "report.PDF") as artifact:
        assert artifact.content_type == "application/pdf"
        assert artifact.extension == "pdf"
        assert artifact.page_count == 2
        assert artifact.dimensions == ((72, 144), (72, 144))
        assert artifact.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("payload", "filename", "code"),
    [
        (b"not an upload", "private-name.exe", "unsupported_file"),
        (b"", "private-name.pdf", "empty_file"),
        (image_bytes("PNG"), "private-name.jpg", "extension_mismatch"),
        (b"\xff\xd8\xffbroken", "private-name.jpg", "unreadable_file"),
        (b"\x89PNG\r\n\x1a\nbroken", "private-name.png", "unreadable_file"),
        (b"%PDF-1.7\nnot-a-pdf", "private-name.pdf", "unreadable_file"),
    ],
)
def test_invalid_inputs_use_stable_codes_without_leaking_claimed_name(payload, filename, code):
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(payload), filename)
    assert raised.value.code == code
    assert "private-name" not in str(raised.value)
    if payload:
        assert payload[:8].hex() not in str(raised.value)


def test_encrypted_pdf_is_rejected_without_trying_a_password():
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(pdf_bytes(encrypted=True)), "locked.pdf")
    assert raised.value.code == "encrypted_pdf"


@pytest.mark.parametrize(
    "unsafe_payload",
    [pdf_bytes(javascript=True), pdf_bytes(attachment=True), pdf_bytes(form_action=True)],
)
def test_active_content_and_attachments_are_rejected(unsafe_payload):
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(unsafe_payload), "unsafe.pdf")
    assert raised.value.code == "unsafe_pdf"


def test_pdf_page_limit_is_enforced_by_the_real_parser():
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(pdf_bytes(pages=61)), "many-pages.pdf")
    assert raised.value.code == "too_many_pages"


def test_pixel_limit_is_checked_before_full_decode(monkeypatch):
    monkeypatch.setattr(inspection, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(image_bytes("PNG", size=(11, 10))), "large.png")
    assert raised.value.code == "pixel_limit"


def test_crafted_oversized_png_header_is_rejected_without_pixel_allocation():
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(oversized_png_header()), "dimension-bomb.png")
    assert raised.value.code == "pixel_limit"


def test_multiframe_png_is_not_silently_flattened():
    with pytest.raises(InspectionError) as raised:
        inspect_upload(NonSeekable(image_bytes("PNG", save_all=True)), "animated.png")
    assert raised.value.code == "multi_frame_image"


def test_size_limit_reads_no_more_than_limit_plus_one(monkeypatch):
    monkeypatch.setattr(inspection, "MAX_IMAGE_BYTES", 8 * 1024)
    stream = EndlessJpeg()
    with pytest.raises(InspectionError) as raised:
        inspect_upload(stream, "oversized.jpg")
    assert raised.value.code == "file_too_large"
    assert stream.total_read == 8 * 1024 + 1


def test_unknown_content_is_rejected_after_the_bounded_sniff_window():
    class EndlessUnknown:
        total_read = 0

        def read(self, size):
            self.total_read += size
            return b"x" * size

    stream = EndlessUnknown()
    with pytest.raises(InspectionError) as raised:
        inspect_upload(stream, "unknown.bin")
    assert raised.value.code == "unsupported_file"
    assert stream.total_read == inspection._SNIFF_BYTES


def test_misbehaving_stream_cannot_return_more_than_requested():
    class Overreader:
        def read(self, size):
            return b"x" * (size + 1)

    with pytest.raises(InspectionError) as raised:
        inspect_upload(Overreader(), "payload.pdf")
    assert raised.value.code == "invalid_stream"


def test_stream_exception_is_collapsed_to_safe_error():
    with pytest.raises(InspectionError) as raised:
        inspect_upload(BrokenStream(), "secret-filename.pdf")
    assert raised.value.code == "unreadable_file"
    assert "secret" not in str(raised.value)
    assert "decoder" not in str(raised.value)


def test_every_failed_inspection_cleans_its_private_temp_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(InspectionError):
        inspect_upload(NonSeekable(image_bytes("PNG")), "wrong.pdf")
    assert list(tmp_path.iterdir()) == []


def test_artifact_close_owns_and_closes_outstanding_replay_handles(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    artifact = inspect_upload(NonSeekable(image_bytes("PNG")), "image.png")
    replay = artifact.open()

    artifact.close()

    assert replay.closed is True
    assert artifact.closed is True
    assert list(tmp_path.iterdir()) == []


def test_pdfium_distribution_retains_its_bundled_license_notices():
    installed_files = {str(path).replace("\\", "/") for path in metadata.files("pypdfium2") or []}
    assert any(".dist-info/licenses/LICENSES/" in path for path in installed_files)
    assert any(".dist-info/licenses/data/" in path for path in installed_files)


def test_project_uses_bounded_permissive_pdf_dependencies_only():
    project = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pypdf>=6.16,<7"' in project
    assert '"pypdfium2>=5.13,<6"' in project
    assert "PyMuPDF" not in project
