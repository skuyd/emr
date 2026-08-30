from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import NonRetryableProcessingError
from .value_objects import InvalidRegion, normalized_polygon


class PreparationError(NonRetryableProcessingError):
    """A preparation failure represented only by a stable, non-sensitive code."""


class PreparedPageKind(str, Enum):
    TEXT_LAYER = "TEXT_LAYER"
    RASTER = "RASTER"


@dataclass(frozen=True)
class PreparedTextSpan:
    text: str
    polygon: tuple[tuple[float, float], ...]

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise InvalidRegion("Prepared text must not be empty")
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "polygon", normalized_polygon(self.polygon))


@dataclass(frozen=True)
class PreparedPage:
    page_number: int
    kind: PreparedPageKind
    width: int
    height: int
    source_width: int
    source_height: int
    source_rotation: int = 0
    raster_path: Path | None = None
    text_spans: tuple[PreparedTextSpan, ...] = ()

    def __post_init__(self):
        try:
            kind = PreparedPageKind(self.kind)
        except (TypeError, ValueError):
            raise ValueError("Unknown prepared page kind") from None
        object.__setattr__(self, "kind", kind)
        for name in ("page_number", "width", "height", "source_width", "source_height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.source_rotation not in {0, 90, 180, 270}:
            raise ValueError("Source rotation must be a right angle")
        spans = tuple(self.text_spans)
        if any(not isinstance(span, PreparedTextSpan) for span in spans):
            raise ValueError("Prepared text spans have an invalid type")
        object.__setattr__(self, "text_spans", spans)
        if kind == PreparedPageKind.TEXT_LAYER:
            if self.raster_path is not None or not spans:
                raise ValueError("Text-layer pages require spans and cannot contain a raster")
        elif self.raster_path is None or spans:
            raise ValueError("Raster pages require a raster path and cannot contain text spans")
        if self.raster_path is not None:
            object.__setattr__(self, "raster_path", Path(self.raster_path))

    @property
    def full_text(self):
        return "\n".join(span.text for span in self.text_spans)

    def open_raster(self):
        if self.kind != PreparedPageKind.RASTER or self.raster_path is None:
            raise PreparationError("raster_not_available")
        try:
            return self.raster_path.open("rb")
        except OSError:
            raise PreparationError("prepared_page_unavailable") from None


class PreparedDocument:
    def __init__(self, owner, pages, *, warnings=()):
        pages = tuple(pages)
        if not pages or [page.page_number for page in pages] != list(range(1, len(pages) + 1)):
            owner.cleanup()
            raise PreparationError("invalid_prepared_document")
        self._owner = owner
        self.pages = pages
        self.warnings = tuple(dict.fromkeys(warnings))
        self._closed = False

    @property
    def extracted_text_layer(self):
        return all(page.kind == PreparedPageKind.TEXT_LAYER for page in self.pages)

    @property
    def closed(self):
        return self._closed

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._owner.cleanup()

    def __enter__(self):
        if self._closed:
            raise PreparationError("prepared_document_closed")
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def prepare_document(source, content_type, *, force_raster=False):
    if content_type == "application/pdf":
        from .pdf import prepare_pdf

        return prepare_pdf(source, force_raster=force_raster)
    if content_type in {"image/jpeg", "image/png", "image/heic"}:
        from .images import prepare_image

        return prepare_image(source, content_type)
    raise PreparationError("unsupported_file")
