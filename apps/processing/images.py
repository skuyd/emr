import io
import math
from pathlib import Path
import tempfile
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_heif

from .preparation import PreparationError, PreparedDocument, PreparedPage, PreparedPageKind


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OCR_PAGE_PIXELS = 40_000_000
_READ_CHUNK_BYTES = 64 * 1024
_EXPECTED_FORMATS = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/heic": {"HEIF", "HEIC"},
}
_ORIENTATION_ROTATION = {3: 180, 5: 90, 6: 90, 7: 270, 8: 270}

pillow_heif.register_heif_opener()


def _read_bounded(source):
    payload = bytearray()
    while True:
        try:
            chunk = source.read(min(_READ_CHUNK_BYTES, MAX_IMAGE_BYTES + 1 - len(payload)))
        except Exception:
            raise PreparationError("unreadable_file") from None
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise PreparationError("unreadable_file")
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > MAX_IMAGE_BYTES:
            raise PreparationError("file_too_large")
    if not payload:
        raise PreparationError("unreadable_file")
    return bytes(payload)


def _flatten_for_ocr(image):
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def prepare_image(source, content_type):
    if content_type not in _EXPECTED_FORMATS:
        raise PreparationError("unsupported_file")
    payload = _read_bounded(source)
    owner = tempfile.TemporaryDirectory(prefix="phr-prepared-image-")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(payload))
            if (image.format or "").upper() not in _EXPECTED_FORMATS[content_type]:
                raise PreparationError("unreadable_file")
            orientation = image.getexif().get(274, 1)
            image.load()
            oriented = ImageOps.exif_transpose(image)
        source_width, source_height = oriented.size
        if source_width <= 0 or source_height <= 0:
            raise PreparationError("unreadable_file")
        prepared_warnings = []
        rotation = _ORIENTATION_ROTATION.get(orientation, 0)
        if orientation not in {None, 1}:
            prepared_warnings.append("orientation_applied")
        raster = _flatten_for_ocr(oriented)
        if raster.width * raster.height > MAX_OCR_PAGE_PIXELS:
            scale = math.sqrt(MAX_OCR_PAGE_PIXELS / (raster.width * raster.height))
            target = (max(1, math.floor(raster.width * scale)), max(1, math.floor(raster.height * scale)))
            raster = raster.resize(target, Image.Resampling.LANCZOS)
            prepared_warnings.append("ocr_image_downscaled")
        raster_path = Path(owner.name) / "page-0001.png"
        raster.save(raster_path, format="PNG", compress_level=6)
        page = PreparedPage(
            page_number=1,
            kind=PreparedPageKind.RASTER,
            width=raster.width,
            height=raster.height,
            source_width=source_width,
            source_height=source_height,
            source_rotation=rotation,
            raster_path=raster_path,
        )
        return PreparedDocument(owner, (page,), warnings=prepared_warnings)
    except PreparationError:
        owner.cleanup()
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        owner.cleanup()
        raise PreparationError("preparation_page_too_large") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        owner.cleanup()
        raise PreparationError("unreadable_file") from None
    except Exception:
        owner.cleanup()
        raise PreparationError("unreadable_file") from None
