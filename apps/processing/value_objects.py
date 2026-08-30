from dataclasses import dataclass
import math
from numbers import Real
from collections.abc import Mapping


class InvalidRegion(ValueError):
    pass


class InvalidOcrPage(ValueError):
    pass


def normalized_polygon(value):
    if isinstance(value, (str, bytes)):
        raise InvalidRegion("A polygon must contain coordinate pairs")
    try:
        points = tuple(value)
    except TypeError:
        raise InvalidRegion("A polygon must contain coordinate pairs") from None
    if not 3 <= len(points) <= 16:
        raise InvalidRegion("A polygon must contain between 3 and 16 points")
    normalized = []
    for point in points:
        if isinstance(point, (str, bytes)):
            raise InvalidRegion("Each polygon point must contain two numbers")
        try:
            coordinates = tuple(point)
        except TypeError:
            raise InvalidRegion("Each polygon point must contain two numbers") from None
        if len(coordinates) != 2:
            raise InvalidRegion("Each polygon point must contain two numbers")
        x, y = coordinates
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, Real)
            or not isinstance(y, Real)
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
            or not 0 <= float(x) <= 1
            or not 0 <= float(y) <= 1
        ):
            raise InvalidRegion("Polygon coordinates must be finite values from zero to one")
        normalized.append((float(x), float(y)))
    area = abs(
        sum(
            normalized[index][0] * normalized[(index + 1) % len(normalized)][1]
            - normalized[(index + 1) % len(normalized)][0] * normalized[index][1]
            for index in range(len(normalized))
        )
        / 2
    )
    if area == 0:
        raise InvalidRegion("A polygon must have a nonzero area")
    return tuple(normalized)


def normalized_confidence(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise InvalidRegion("Confidence must be a finite number")
    value = float(value)
    if not 0 <= value <= 1:
        raise InvalidRegion("Confidence must be from zero to one")
    return value


def _required_text(value, label, error_type):
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} must not be empty")
    normalized = value.strip()
    if len(normalized) > 100_000:
        raise error_type(f"{label} is too long")
    return normalized


@dataclass(frozen=True)
class OcrRegion:
    text: str
    polygon: tuple[tuple[float, float], ...]
    confidence: float
    reading_order: int = 0

    def __post_init__(self):
        object.__setattr__(self, "text", _required_text(self.text, "OCR text", InvalidRegion))
        object.__setattr__(self, "polygon", normalized_polygon(self.polygon))
        object.__setattr__(self, "confidence", normalized_confidence(self.confidence))
        if isinstance(self.reading_order, bool) or not isinstance(self.reading_order, int) or self.reading_order < 0:
            raise InvalidRegion("Reading order must be a nonnegative integer")


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    width: int
    height: int
    regions: tuple[OcrRegion, ...]
    provider: str
    provider_version: str
    provider_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        for name in ("page_number", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidOcrPage(f"{name} must be a positive integer")
        provider = _required_text(self.provider, "OCR provider", InvalidOcrPage)
        provider_version = _required_text(self.provider_version, "OCR provider version", InvalidOcrPage)
        metadata_items = self.provider_metadata.items() if isinstance(self.provider_metadata, Mapping) else self.provider_metadata
        try:
            metadata = tuple(metadata_items)
        except TypeError:
            raise InvalidOcrPage("OCR provider metadata must contain key-value pairs") from None
        if any(
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
            for item in metadata
        ):
            raise InvalidOcrPage("OCR provider metadata must contain nonempty string key-value pairs")
        if len({item[0] for item in metadata}) != len(metadata):
            raise InvalidOcrPage("OCR provider metadata keys must be unique")
        try:
            regions = tuple(self.regions)
        except TypeError:
            raise InvalidOcrPage("OCR regions must be iterable") from None
        if any(not isinstance(region, OcrRegion) for region in regions):
            raise InvalidOcrPage("Every OCR page region must be an OcrRegion")
        orders = [region.reading_order for region in regions]
        if len(orders) != len(set(orders)):
            raise InvalidOcrPage("OCR region reading order must be unique within a page")
        object.__setattr__(self, "regions", tuple(sorted(regions, key=lambda region: region.reading_order)))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "provider_version", provider_version)
        object.__setattr__(self, "provider_metadata", tuple(sorted((str(key), str(value)) for key, value in metadata)))

    @property
    def full_text(self):
        return "\n".join(region.text for region in self.regions)
