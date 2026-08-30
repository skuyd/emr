import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
import warnings

from PIL import Image, UnidentifiedImageError
import pillow_heif
from pypdf import PdfReader
import pypdfium2

from .errors import ArtifactClosed, InspectionError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 60
MAX_IMAGE_PIXELS = 80_000_000
MAX_IMAGE_DIMENSION = 65_535
MAX_PDF_PAGE_POINTS = 20_000
_READ_CHUNK_BYTES = 64 * 1024
_SNIFF_BYTES = 4 * 1024
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_IMAGE_FORMATS = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/heic": {"HEIF", "HEIC"},
}
_EXTENSIONS = {
    "application/pdf": {"pdf"},
    "image/jpeg": {"jpg", "jpeg"},
    "image/png": {"png"},
    "image/heic": {"heic"},
}
_NORMALIZED_EXTENSION = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
}
_UNSAFE_ACTIONS = {
    "/JavaScript",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/GoToR",
    "/Rendition",
    "/Sound",
    "/Movie",
}
_UNSAFE_ANNOTATIONS = {"/FileAttachment", "/RichMedia", "/Sound", "/Movie", "/Screen", "/3D"}

pillow_heif.register_heif_opener()


class InspectedFile:
    """Owns the exact, private bytes validated by :func:`inspect_upload`."""

    def __init__(
        self,
        owner,
        path,
        *,
        content_type,
        extension,
        byte_size,
        sha256,
        page_count,
        dimensions,
    ):
        self._owner = owner
        self._path = Path(path)
        self.content_type = content_type
        self.extension = extension
        self.byte_size = byte_size
        self.sha256 = sha256
        self.page_count = page_count
        self.dimensions = tuple(dimensions)
        self._closed = False
        self._handles = []

    @property
    def closed(self):
        return self._closed

    def open(self):
        if self._closed:
            raise ArtifactClosed()
        try:
            handle = self._path.open("rb")
            self._handles.append(handle)
            return handle
        except OSError:
            raise ArtifactClosed() from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        for handle in self._handles:
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        self._owner.cleanup()

    def __enter__(self):
        if self._closed:
            raise ArtifactClosed()
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _claimed_extension(claimed_name):
    if not isinstance(claimed_name, str):
        return ""
    basename = PurePosixPath(claimed_name.replace("\\", "/")).name
    if not basename or "." not in basename:
        return ""
    return basename.rsplit(".", 1)[-1].casefold()


def _detect_content_type(header):
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b"%PDF-" in header[:1024]:
        return "application/pdf"
    if len(header) >= 16 and header[4:8] == b"ftyp":
        box_size = int.from_bytes(header[:4], "big")
        brand_bytes = header[8 : min(len(header), box_size or len(header))]
        brands = {brand_bytes[index : index + 4] for index in range(0, len(brand_bytes) - 3, 4)}
        if brands & _HEIF_BRANDS:
            return "image/heic"
    return None


def _copy_bounded(stream, target):
    digest = hashlib.sha256()
    size = 0
    header = b""
    content_type = None
    limit = MAX_PDF_BYTES

    while True:
        remaining = (limit if content_type is not None else MAX_PDF_BYTES) + 1 - size
        if remaining <= 0:
            raise InspectionError("file_too_large")
        request_size = min(_SNIFF_BYTES if content_type is None else _READ_CHUNK_BYTES, remaining)
        try:
            chunk = stream.read(request_size)
        except Exception:
            raise InspectionError("unreadable_file") from None
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise InspectionError("invalid_stream")
        chunk = bytes(chunk)
        if len(chunk) > request_size:
            raise InspectionError("invalid_stream")
        if not chunk:
            break
        size += len(chunk)
        if len(header) < _SNIFF_BYTES:
            header += chunk[: _SNIFF_BYTES - len(header)]
        if content_type is None:
            content_type = _detect_content_type(header)
            if content_type is not None:
                limit = MAX_PDF_BYTES if content_type == "application/pdf" else MAX_IMAGE_BYTES
            elif len(header) >= _SNIFF_BYTES:
                raise InspectionError("unsupported_file")
        if size > limit:
            raise InspectionError("file_too_large")
        digest.update(chunk)
        try:
            target.write(chunk)
        except OSError:
            raise InspectionError("inspection_unavailable") from None

    if size == 0:
        raise InspectionError("empty_file")
    content_type = content_type or _detect_content_type(header)
    if content_type is None:
        raise InspectionError("unsupported_file")
    return content_type, size, digest.hexdigest()


def _resolved(value):
    for _ in range(8):
        get_object = getattr(value, "get_object", None)
        if get_object is None:
            break
        resolved = get_object()
        if resolved is value:
            break
        value = resolved
    return value


def _unsafe_action(value):
    value = _resolved(value)
    if not hasattr(value, "get"):
        return False
    action_type = str(value.get("/S", ""))
    if action_type in _UNSAFE_ACTIONS or (action_type and action_type not in {"/GoTo", "/URI", "/Named"}):
        return True
    next_action = value.get("/Next")
    if next_action is None:
        return False
    next_action = _resolved(next_action)
    if isinstance(next_action, (list, tuple)):
        return any(_unsafe_action(item) for item in next_action)
    return _unsafe_action(next_action)


def _assert_safe_form(value, seen=None, depth=0):
    if seen is None:
        seen = set()
    if depth > 32:
        raise InspectionError("unsafe_pdf")
    value = _resolved(value)
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_form(child, seen, depth + 1)
        return
    if not hasattr(value, "get"):
        return
    if any(value.get(key) is not None for key in ("/AA", "/JS", "/EF", "/AF", "/RichMediaContent")):
        raise InspectionError("unsafe_pdf")
    if str(value.get("/Subtype", "")) in _UNSAFE_ANNOTATIONS or _unsafe_action(value.get("/A")):
        raise InspectionError("unsafe_pdf")
    for key in ("/Fields", "/Kids"):
        child = value.get(key)
        if child is not None:
            _assert_safe_form(child, seen, depth + 1)


def _assert_safe_pdf(reader):
    root = _resolved(reader.trailer.get("/Root"))
    if not hasattr(root, "get"):
        raise InspectionError("unreadable_file")
    if root.get("/OpenAction") is not None or root.get("/AA") is not None or root.get("/AF") is not None:
        raise InspectionError("unsafe_pdf")

    names = _resolved(root.get("/Names"))
    if hasattr(names, "get") and (names.get("/EmbeddedFiles") is not None or names.get("/JavaScript") is not None):
        raise InspectionError("unsafe_pdf")
    form = _resolved(root.get("/AcroForm"))
    if hasattr(form, "get") and form.get("/XFA") is not None:
        raise InspectionError("unsafe_pdf")
    if form is not None:
        _assert_safe_form(form)

    for page in reader.pages:
        if page.get("/AA") is not None or page.get("/AF") is not None:
            raise InspectionError("unsafe_pdf")
        annotations = _resolved(page.get("/Annots", []))
        if not isinstance(annotations, (list, tuple)):
            continue
        for annotation in annotations:
            annotation = _resolved(annotation)
            if not hasattr(annotation, "get"):
                continue
            if str(annotation.get("/Subtype", "")) in _UNSAFE_ANNOTATIONS or annotation.get("/FS") is not None:
                raise InspectionError("unsafe_pdf")
            if annotation.get("/AA") is not None or _unsafe_action(annotation.get("/A")):
                raise InspectionError("unsafe_pdf")


def _inspect_pdf(path):
    try:
        with path.open("rb") as source:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise InspectionError("encrypted_pdf")
            page_count = len(reader.pages)
            if page_count == 0:
                raise InspectionError("empty_pdf")
            if page_count > MAX_PDF_PAGES:
                raise InspectionError("too_many_pages")
            _assert_safe_pdf(reader)

        pdf = pypdfium2.PdfDocument(str(path))
        dimensions = []
        try:
            if len(pdf) != page_count:
                raise InspectionError("unreadable_file")
            for index in range(page_count):
                page = pdf.get_page(index)
                try:
                    width, height = page.get_size()
                    if (
                        not math.isfinite(width)
                        or not math.isfinite(height)
                        or width <= 0
                        or height <= 0
                        or width > MAX_PDF_PAGE_POINTS
                        or height > MAX_PDF_PAGE_POINTS
                    ):
                        raise InspectionError("invalid_page_dimensions")
                    scale = min(0.25, 64 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        bitmap.to_pil().load()
                    finally:
                        bitmap.close()
                    dimensions.append((max(1, math.ceil(width)), max(1, math.ceil(height))))
                finally:
                    page.close()
        finally:
            pdf.close()
        return page_count, dimensions
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("unreadable_file") from None


def _inspect_image(path, content_type):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in _IMAGE_FORMATS[content_type]:
                    raise InspectionError("content_mismatch")
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise InspectionError("invalid_image_dimensions")
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                    raise InspectionError("pixel_limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise InspectionError("multi_frame_image")
                image.verify()
            with Image.open(path) as image:
                image.load()
    except InspectionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InspectionError("pixel_limit") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise InspectionError("unreadable_file") from None
    except Exception:
        raise InspectionError("unreadable_file") from None
    return 1, [(width, height)]


def inspect_upload(stream, claimed_name):
    """Inspect a non-seekable upload and return an owned replayable artifact.

    The caller owns a successful result and must close it (normally with a
    ``with`` block). Every failure removes the temporary artifact.
    """

    try:
        owner = tempfile.TemporaryDirectory(prefix="phr-inspect-")
        path = Path(owner.name) / "artifact"
        with path.open("xb") as target:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            content_type, byte_size, sha256 = _copy_bounded(stream, target)

        claimed_extension = _claimed_extension(claimed_name)
        if claimed_extension not in _EXTENSIONS[content_type]:
            raise InspectionError("extension_mismatch")
        if content_type == "application/pdf":
            page_count, dimensions = _inspect_pdf(path)
        else:
            page_count, dimensions = _inspect_image(path, content_type)
        return InspectedFile(
            owner,
            path,
            content_type=content_type,
            extension=_NORMALIZED_EXTENSION[content_type],
            byte_size=byte_size,
            sha256=sha256,
            page_count=page_count,
            dimensions=dimensions,
        )
    except InspectionError:
        if "owner" in locals():
            owner.cleanup()
        raise
    except Exception:
        if "owner" in locals():
            owner.cleanup()
        raise InspectionError("inspection_unavailable") from None
