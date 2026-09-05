import io
import math
from pathlib import Path
import tempfile
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_heif
import pypdfium2

from apps.core.pdfium import PDFIUM_LOCK


pillow_heif.register_heif_opener()

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RENDER_PIXELS = 20_000_000
PDF_RENDER_SCALE = 2.0
THUMBNAIL_BOX = (160, 220)
MAX_THUMBNAIL_PAGES = 60
_CHUNK_BYTES = 64 * 1024


class PreviewUnavailable(ValueError):
    pass


def _copy_bounded(source, target, limit):
    size = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = source.read(min(_CHUNK_BYTES, limit + 1 - size))
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise PreviewUnavailable()
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise PreviewUnavailable()
                output.write(bytes(chunk))
    except PreviewUnavailable:
        raise
    except Exception:
        raise PreviewUnavailable() from None
    if size == 0:
        raise PreviewUnavailable()


def _png_bytes(image, *, thumbnail):
    image = image.convert("RGB")
    if thumbnail:
        image.thumbnail(THUMBNAIL_BOX, Image.Resampling.LANCZOS)
    elif image.width * image.height > MAX_RENDER_PIXELS:
        scale = math.sqrt(MAX_RENDER_PIXELS / (image.width * image.height))
        image = image.resize(
            (max(1, math.floor(image.width * scale)), max(1, math.floor(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()


def _thumbnail_cell(image):
    image = image.convert("RGB")
    image.thumbnail(THUMBNAIL_BOX, Image.Resampling.LANCZOS)
    cell = Image.new("RGB", THUMBNAIL_BOX, "white")
    cell.paste(image, ((THUMBNAIL_BOX[0] - image.width) // 2, (THUMBNAIL_BOX[1] - image.height) // 2))
    return cell


def _sheet_bytes(cells):
    sheet = Image.new("RGB", (THUMBNAIL_BOX[0], THUMBNAIL_BOX[1] * len(cells)), "white")
    for index, cell in enumerate(cells):
        sheet.paste(cell, (0, THUMBNAIL_BOX[1] * index))
    output = io.BytesIO()
    sheet.save(output, format="PNG", compress_level=7)
    return output.getvalue()


def _render_pdf(source, page_number, *, thumbnail):
    with tempfile.TemporaryDirectory(prefix="phr-viewer-") as directory:
        path = Path(directory) / "source.pdf"
        _copy_bounded(source, path, MAX_PDF_BYTES)
        with PDFIUM_LOCK:
            document = None
            page = None
            bitmap = None
            try:
                document = pypdfium2.PdfDocument(str(path))
                if not 1 <= page_number <= len(document):
                    raise PreviewUnavailable()
                page = document[page_number - 1]
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise PreviewUnavailable()
                if thumbnail:
                    scale = min(THUMBNAIL_BOX[0] / width, THUMBNAIL_BOX[1] / height)
                else:
                    scale = min(PDF_RENDER_SCALE, math.sqrt(MAX_RENDER_PIXELS / (width * height)))
                bitmap = page.render(scale=max(0.1, scale))
                with bitmap.to_pil() as rendered:
                    return _png_bytes(rendered, thumbnail=thumbnail)
            except PreviewUnavailable:
                raise
            except Exception:
                raise PreviewUnavailable() from None
            finally:
                try:
                    if bitmap is not None:
                        bitmap.close()
                finally:
                    try:
                        if page is not None:
                            page.close()
                    finally:
                        if document is not None:
                            document.close()


def _render_image(source, *, thumbnail):
    payload = bytearray()
    try:
        while True:
            chunk = source.read(min(_CHUNK_BYTES, MAX_IMAGE_BYTES + 1 - len(payload)))
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise PreviewUnavailable()
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_IMAGE_BYTES:
                raise PreviewUnavailable()
        if not payload:
            raise PreviewUnavailable()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source_image:
                source_image.load()
                image = ImageOps.exif_transpose(source_image)
                return _png_bytes(image, thumbnail=thumbnail)
    except PreviewUnavailable:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError):
        raise PreviewUnavailable() from None


def render_page(source, content_type, page_number, *, thumbnail=False):
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise PreviewUnavailable()
    if content_type == "application/pdf":
        return _render_pdf(source, page_number, thumbnail=thumbnail)
    if content_type in {"image/jpeg", "image/png", "image/heic"} and page_number == 1:
        return _render_image(source, thumbnail=thumbnail)
    raise PreviewUnavailable()


def render_thumbnail_sheet(source, content_type, page_count):
    if not isinstance(page_count, int) or isinstance(page_count, bool) or not 1 <= page_count <= MAX_THUMBNAIL_PAGES:
        raise PreviewUnavailable()
    if content_type != "application/pdf":
        if page_count != 1:
            raise PreviewUnavailable()
        payload = _render_image(source, thumbnail=True)
        with Image.open(io.BytesIO(payload)) as image:
            return _sheet_bytes((_thumbnail_cell(image),))

    with tempfile.TemporaryDirectory(prefix="phr-viewer-sheet-") as directory:
        path = Path(directory) / "source.pdf"
        _copy_bounded(source, path, MAX_PDF_BYTES)
        with PDFIUM_LOCK:
            document = None
            try:
                document = pypdfium2.PdfDocument(str(path))
                if len(document) != page_count:
                    raise PreviewUnavailable()
                cells = []
                for index in range(page_count):
                    page = document[index]
                    bitmap = None
                    try:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0:
                            raise PreviewUnavailable()
                        scale = max(0.1, min(THUMBNAIL_BOX[0] / width, THUMBNAIL_BOX[1] / height))
                        bitmap = page.render(scale=scale)
                        with bitmap.to_pil() as rendered:
                            cells.append(_thumbnail_cell(rendered))
                    finally:
                        try:
                            if bitmap is not None:
                                bitmap.close()
                        finally:
                            page.close()
                return _sheet_bytes(cells)
            except PreviewUnavailable:
                raise
            except Exception:
                raise PreviewUnavailable() from None
            finally:
                if document is not None:
                    document.close()
