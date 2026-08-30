import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from apps.processing.preparation import PreparationError, PreparedPageKind
from apps.processing.pdf import prepare_pdf, text_layer_is_trustworthy


def _pdf_bytes(texts, *, rotations=None, page_size=(612, 792), encrypt=False):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    rotations = rotations or [0] * len(texts)
    for text, rotation in zip(texts, rotations, strict=True):
        page = writer.add_blank_page(width=page_size[0], height=page_size[1])
        if text is not None:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
        if rotation:
            page.rotate(rotation)
    if encrypt:
        writer.encrypt("synthetic-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_trustworthy_pdf_text_layer_is_preserved_with_normalized_source_boxes():
    payload = _pdf_bytes(["Synthetic laboratory result text layer 12345"])

    with prepare_pdf(io.BytesIO(payload)) as prepared:
        page = prepared.pages[0]
        assert prepared.extracted_text_layer is True
        assert page.kind == PreparedPageKind.TEXT_LAYER
        assert page.raster_path is None
        assert "Synthetic laboratory result" in page.full_text
        assert page.text_spans
        assert all(0 <= coordinate <= 1 for point in page.text_spans[0].polygon for coordinate in point)


def test_text_layer_policy_requires_twenty_meaningful_characters_and_seventy_percent_printable():
    assert text_layer_is_trustworthy("A" * 20)
    assert text_layer_is_trustworthy("A" * 20 + "\x01" * 8)
    assert not text_layer_is_trustworthy("A" * 19)
    assert not text_layer_is_trustworthy("A" * 20 + "\x01" * 9)


@pytest.mark.parametrize("text", [None, "short", "control replacement \ufffd text only"])
def test_missing_short_or_gibberish_text_layer_renders_page_for_ocr(text):
    if text is not None and not text.isascii():
        payload = _pdf_bytes([None])
    else:
        payload = _pdf_bytes([text])

    with prepare_pdf(io.BytesIO(payload)) as prepared:
        page = prepared.pages[0]
        assert prepared.extracted_text_layer is False
        assert page.kind == PreparedPageKind.RASTER
        assert page.raster_path is not None and page.raster_path.is_file()
        assert page.open_raster().read(8) == b"\x89PNG\r\n\x1a\n"
        assert "text_layer_unusable_page_1" in prepared.warnings


def test_mixed_multi_page_pdf_uses_text_per_page_and_preserves_page_numbers():
    payload = _pdf_bytes(["A complete synthetic text layer with enough characters", None])

    with prepare_pdf(io.BytesIO(payload)) as prepared:
        assert [page.page_number for page in prepared.pages] == [1, 2]
        assert [page.kind for page in prepared.pages] == [PreparedPageKind.TEXT_LAYER, PreparedPageKind.RASTER]
        assert prepared.extracted_text_layer is False


def test_rotated_pdf_page_is_rendered_with_rotation_recorded():
    payload = _pdf_bytes(["A complete synthetic text layer with enough characters"], rotations=[90])

    with prepare_pdf(io.BytesIO(payload)) as prepared:
        page = prepared.pages[0]
        assert page.kind == PreparedPageKind.RASTER
        assert page.source_rotation == 90
        assert page.width > page.height


def test_encrypted_pdf_is_rejected_with_stable_non_sensitive_code():
    payload = _pdf_bytes(["A complete synthetic text layer with enough characters"], encrypt=True)

    with pytest.raises(PreparationError) as error:
        prepare_pdf(io.BytesIO(payload))
    assert error.value.code == "encrypted_pdf"
    assert "synthetic-password" not in str(error.value)


def test_pdf_render_limits_are_checked_before_large_bitmap_allocation():
    payload = _pdf_bytes([None], page_size=(3000, 3000))

    with pytest.raises(PreparationError) as error:
        prepare_pdf(io.BytesIO(payload))
    assert error.value.code == "preparation_page_too_large"


def test_pdf_page_count_limit_is_enforced_before_rendering():
    payload = _pdf_bytes(["enough synthetic characters for a trusted text layer"] * 61)

    with pytest.raises(PreparationError) as error:
        prepare_pdf(io.BytesIO(payload))
    assert error.value.code == "too_many_pages"


def test_pdf_document_pixel_budget_is_preflighted_before_rendering(monkeypatch):
    payload = _pdf_bytes([None, None])
    monkeypatch.setattr("apps.processing.pdf.MAX_OCR_PAGE_PIXELS", 4_000_000)
    monkeypatch.setattr("apps.processing.pdf.MAX_OCR_DOCUMENT_PIXELS", 5_000_000)

    with pytest.raises(PreparationError) as error:
        prepare_pdf(io.BytesIO(payload))
    assert error.value.code == "preparation_document_too_large"
