from dataclasses import dataclass, field
import hashlib
import re
import unicodedata

from apps.processing.value_objects import InvalidRegion, OcrPage, normalized_polygon
from tools.sample_dictionary.normalize import is_rejected_candidate_name, normalize_candidate_name, result_token


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class LabCandidate:
    raw_name: str
    normalized_name: str
    source_file_hash: str
    page: int
    region: tuple[tuple[float, float], ...]
    context_hash: str
    _sort_key: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.raw_name, str) or not self.raw_name.strip():
            raise ValueError("Candidate raw name must not be empty")
        if not isinstance(self.normalized_name, str) or not self.normalized_name.strip():
            raise ValueError("Candidate normalized name must not be empty")
        if _DIGEST.fullmatch(self.source_file_hash) is None or _DIGEST.fullmatch(self.context_hash) is None:
            raise ValueError("Candidate provenance requires SHA-256 digests")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page <= 0:
            raise ValueError("Candidate page must be positive")
        try:
            region = normalized_polygon(self.region)
        except InvalidRegion as error:
            raise ValueError("Candidate region is invalid") from error
        object.__setattr__(self, "raw_name", self.raw_name.strip())
        object.__setattr__(self, "normalized_name", self.normalized_name.strip())
        object.__setattr__(self, "region", region)
        object.__setattr__(
            self,
            "_sort_key",
            (self.source_file_hash, self.page, self.normalized_name.casefold(), region),
        )

    def public_record(self):
        return {
            "context_hash": self.context_hash,
            "normalized_name": self.normalized_name,
            "page": self.page,
            "raw_name": self.raw_name,
            "region": [[x, y] for x, y in self.region],
            "source_file_hash": self.source_file_hash,
        }


def _bounds(region):
    xs = [point[0] for point in region.polygon]
    ys = [point[1] for point in region.polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _same_row(first, second):
    _fx1, fy1, _fx2, fy2 = _bounds(first)
    _sx1, sy1, _sx2, sy2 = _bounds(second)
    overlap = max(0.0, min(fy2, sy2) - max(fy1, sy1))
    shorter = max(1e-9, min(fy2 - fy1, sy2 - sy1))
    centers = abs((fy1 + fy2) / 2 - (sy1 + sy2) / 2)
    return overlap / shorter >= 0.4 or centers <= max(fy2 - fy1, sy2 - sy1) * 0.6


def _rows(page):
    ordered = sorted(page.regions, key=lambda region: (_bounds(region)[1], _bounds(region)[0], region.reading_order))
    rows = []
    for region in ordered:
        best = None
        best_distance = None
        center = sum(point[1] for point in region.polygon) / len(region.polygon)
        for row in rows:
            if not _same_row(row[0], region):
                continue
            row_center = sum(point[1] for item in row for point in item.polygon) / sum(
                len(item.polygon) for item in row
            )
            distance = abs(center - row_center)
            if best_distance is None or distance < best_distance:
                best, best_distance = row, distance
        if best is None:
            rows.append([region])
        else:
            best.append(region)
    for row in rows:
        row.sort(key=lambda region: (_bounds(region)[0], region.reading_order))
    return rows


def _union_polygon(regions):
    boxes = [_bounds(region) for region in regions]
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _name_regions_and_text(row):
    from .extraction import _result_and_tail

    for index, region in enumerate(row):
        if result_token(region.text) or _result_and_tail(region.text):
            if index == 0:
                return (), ""
            name_regions = row[:index]
            return tuple(name_regions), " ".join(item.text.strip() for item in name_regions)
    combined = " ".join(region.text.strip() for region in row)
    normalized = normalize_candidate_name(combined)
    if normalized and normalized != normalize_candidate_name(combined, strip_result=False):
        return (row[0],), normalized
    return (), ""


def extract_lab_candidates(pages, source_file_hash):
    from .layout import associated_rows

    if _DIGEST.fullmatch(source_file_hash) is None:
        raise ValueError("Source alias must be a SHA-256 digest")
    candidates = []
    for association in associated_rows(pages):
        page, row = association.page, association.regions
        if association.fields:
            if not association.fields.get('raw_value') and not association.fields.get('raw_unit'):
                continue
            name_regions = association.fields.get("raw_name", ())
            raw_name = " ".join(region.text.strip() for region in name_regions)
        else:
            name_regions, raw_name = _name_regions_and_text(row)
        normalized = normalize_candidate_name(raw_name)
        public_raw_name = normalize_candidate_name(raw_name, strip_result=False)
        if (
            not name_regions
            or not normalized
            or is_rejected_candidate_name(normalized)
            or is_rejected_candidate_name(public_raw_name)
        ):
            continue
        context = unicodedata.normalize("NFKC", " ".join(region.text.strip() for region in row))
        candidates.append(
            LabCandidate(
                raw_name=public_raw_name,
                normalized_name=normalized,
                source_file_hash=source_file_hash,
                page=page.page_number,
                region=_union_polygon(name_regions),
                context_hash=hashlib.sha256(context.encode("utf-8")).hexdigest(),
            )
        )
    unique = {candidate._sort_key: candidate for candidate in candidates}
    return tuple(sorted(unique.values(), key=lambda candidate: candidate._sort_key))
