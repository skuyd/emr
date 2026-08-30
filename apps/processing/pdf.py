import math
from pathlib import Path
import tempfile

from pypdf import PdfReader
import pypdfium2

from .preparation import (
    PreparationError,
    PreparedDocument,
    PreparedPage,
    PreparedPageKind,
    PreparedTextSpan,
)


MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 60
PDF_RENDER_DPI = 200
MAX_OCR_PAGE_PIXELS = 40_000_000
MAX_OCR_DOCUMENT_PIXELS = 300_000_000
MIN_MEANINGFUL_CHARACTERS = 20
MIN_PRINTABLE_RATIO = 0.70
_READ_CHUNK_BYTES = 64 * 1024


def _copy_bounded(source, path):
    size = 0
    try:
        with path.open("xb") as target:
            while True:
                chunk = source.read(min(_READ_CHUNK_BYTES, MAX_PDF_BYTES + 1 - size))
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise PreparationError("unreadable_file")
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise PreparationError("file_too_large")
                target.write(bytes(chunk))
    except PreparationError:
        raise
    except Exception:
        raise PreparationError("unreadable_file") from None
    if size == 0:
        raise PreparationError("unreadable_file")


def text_layer_is_trustworthy(text):
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return False
    printable = [character for character in visible if character.isprintable() and character != "\ufffd"]
    meaningful = [character for character in printable if character.isalnum()]
    return len(printable) / len(visible) >= MIN_PRINTABLE_RATIO and len(meaningful) >= MIN_MEANINGFUL_CHARACTERS


def _clamp(value):
    return max(0.0, min(1.0, float(value)))


def _polygon_for_rect(rect, width, height):
    left, bottom, right, top = rect
    x1, x2 = sorted((_clamp(left / width), _clamp(right / width)))
    y1, y2 = sorted((_clamp((height - top) / height), _clamp((height - bottom) / height)))
    if x1 == x2 or y1 == y2:
        return None
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


def _text_spans(text_page, full_text, width, height):
    spans = []
    try:
        count = text_page.count_rects()
        for index in range(count):
            rect = text_page.get_rect(index)
            text = text_page.get_text_bounded(*rect).strip()
            polygon = _polygon_for_rect(rect, width, height)
            if text and polygon:
                spans.append(PreparedTextSpan(text=text, polygon=polygon))
    except Exception:
        spans = []
    if spans:
        return tuple(spans)

    boxes = []
    try:
        for index in range(text_page.count_chars()):
            character = text_page.get_text_range(index, 1)
            if character and not character.isspace():
                boxes.append(text_page.get_charbox(index, loose=True))
    except Exception:
        boxes = []
    if boxes:
        union = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        polygon = _polygon_for_rect(union, width, height)
        if polygon:
            return (PreparedTextSpan(text=full_text, polygon=polygon),)
    return ()


def _page_blueprints(document, *, force_raster=False):
    blueprints = []
    total_raster_pixels = 0
    for index in range(len(document)):
        page = document[index]
        text_page = None
        try:
            width, height = page.get_size()
            raw_rotation = int(page.get_rotation())
            rotation = raw_rotation % 360 if raw_rotation in {0, 90, 180, 270} else raw_rotation % 4 * 90
            if width <= 0 or height <= 0:
                raise PreparationError("unreadable_file")
            text_page = page.get_textpage()
            full_text = text_page.get_text_range().strip()
            spans = _text_spans(text_page, full_text, width, height) if full_text else ()
            trustworthy_text = rotation == 0 and text_layer_is_trustworthy(full_text) and bool(spans)
            use_text = trustworthy_text and not force_raster
            pixel_width = max(1, math.ceil(width * PDF_RENDER_DPI / 72))
            pixel_height = max(1, math.ceil(height * PDF_RENDER_DPI / 72))
            pixels = pixel_width * pixel_height
            if not use_text:
                if pixels > MAX_OCR_PAGE_PIXELS:
                    raise PreparationError("preparation_page_too_large")
                total_raster_pixels += pixels
                if total_raster_pixels > MAX_OCR_DOCUMENT_PIXELS:
                    raise PreparationError("preparation_document_too_large")
            blueprints.append(
                {
                    "page_number": index + 1,
                    "width_points": max(1, round(width)),
                    "height_points": max(1, round(height)),
                    "rotation": rotation,
                    "spans": spans if use_text else (),
                    "warning": "text_layer_bypassed" if trustworthy_text and force_raster else "text_layer_unusable",
                    "pixel_width": pixel_width,
                    "pixel_height": pixel_height,
                }
            )
        finally:
            if text_page is not None:
                text_page.close()
            page.close()
    return blueprints


def _render_page(document, index, output_path):
    page = document[index]
    bitmap = None
    try:
        bitmap = page.render(scale=PDF_RENDER_DPI / 72)
        image = bitmap.to_pil().convert("RGB")
        image.save(output_path, format="PNG", compress_level=6)
        return image.size
    except Exception:
        raise PreparationError("unreadable_file") from None
    finally:
        if bitmap is not None:
            bitmap.close()
        page.close()


def prepare_pdf(source, *, force_raster=False):
    owner = tempfile.TemporaryDirectory(prefix="phr-prepared-pdf-")
    input_path = Path(owner.name) / "source.pdf"
    document = None
    try:
        _copy_bounded(source, input_path)
        with input_path.open("rb") as handle:
            reader = PdfReader(handle, strict=True)
            if reader.is_encrypted:
                raise PreparationError("encrypted_pdf")
            page_count = len(reader.pages)
        if not 1 <= page_count <= MAX_PDF_PAGES:
            raise PreparationError("too_many_pages" if page_count > MAX_PDF_PAGES else "unreadable_file")
        document = pypdfium2.PdfDocument(str(input_path))
        if len(document) != page_count:
            raise PreparationError("unreadable_file")
        blueprints = _page_blueprints(document, force_raster=force_raster)
        pages = []
        prepared_warnings = []
        for index, blueprint in enumerate(blueprints):
            if blueprint["spans"]:
                pages.append(
                    PreparedPage(
                        page_number=blueprint["page_number"],
                        kind=PreparedPageKind.TEXT_LAYER,
                        width=blueprint["width_points"],
                        height=blueprint["height_points"],
                        source_width=blueprint["width_points"],
                        source_height=blueprint["height_points"],
                        source_rotation=blueprint["rotation"],
                        text_spans=blueprint["spans"],
                    )
                )
                continue
            output_path = Path(owner.name) / f"page-{index + 1:04d}.png"
            width, height = _render_page(document, index, output_path)
            pages.append(
                PreparedPage(
                    page_number=blueprint["page_number"],
                    kind=PreparedPageKind.RASTER,
                    width=width,
                    height=height,
                    source_width=blueprint["width_points"],
                    source_height=blueprint["height_points"],
                    source_rotation=blueprint["rotation"],
                    raster_path=output_path,
                )
            )
            prepared_warnings.append(f"{blueprint['warning']}_page_{index + 1}")
        document.close()
        document = None
        return PreparedDocument(owner, pages, warnings=prepared_warnings)
    except PreparationError:
        if document is not None:
            document.close()
        owner.cleanup()
        raise
    except Exception:
        if document is not None:
            document.close()
        owner.cleanup()
        raise PreparationError("unreadable_file") from None
