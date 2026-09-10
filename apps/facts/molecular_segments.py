"""Explicit molecular report boundaries over untouched OCR source locations."""
from dataclasses import dataclass
import re

from .clinical_segments import Segment, _box, _lines, logical_lines
from .pathology_segments import matching_text

SEGMENTER_VERSION = "molecular-segments-v1"
TITLE = re.compile(r"(?:肿瘤)?(?:分子(?:病理)?检测|基因检测|基因测序|分子诊断|NGS检测)(?:结果)?报告(?:单|书)?", re.I)
OTHER = re.compile(r"^(?:[^：。；]{1,40}(?:医院|中心|科))?(?:(?:CT|MR|超声|检验|病理|免疫组化).{0,12}报告|入院记录|出院记录|出院小结)", re.I)
IDENTIFIER = re.compile(r"(?:报告编号|报告号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})")
CONTINUATION = re.compile(r"续(?:上)?页|第\s*[2-9]\d*\s*页\s*[/／]\s*共\s*\d+\s*页")
METADATA = re.compile(r"^(?:标本编号|样本编号|报告编号|采样日期|收样日期|报告日期)[:：]")


@dataclass
class MolecularSegment(Segment):
    routing_kind: str = "MOLECULAR"


def primary_title(text):
    normalized = matching_text(text)
    match = TITLE.search(normalized)
    if not match:
        return None
    before, after = normalized[:match.start()], normalized[match.end():]
    if before and not re.fullmatch(r"[^：。；]{1,40}(?:医院|中心|科)", before):
        return None
    if after and not re.fullmatch(r"[(（][^。；:：]{1,30}[)）]", after):
        return None
    return match.group()


def _lanes(rows):
    anchors = [(r, _box(r)) for r in rows if len(r.text.splitlines()) == 1 and primary_title(r.text) and _box(r)]
    if not any(a.pk != b.pk and abs(left[1] - right[1]) < .08 and (left[2] < right[0] or right[2] < left[0])
               for a, left in anchors for b, right in anchors):
        return [(rows, [])]
    centers = sorted({(b[0] + b[2]) / 2 for _, b in anchors})
    cuts = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    lanes, unplaced = [[] for _ in centers], False
    for row in rows:
        box = _box(row)
        if box is None or any(box[0] < cut < box[2] for cut in cuts):
            unplaced = True
            continue
        lanes[sum((box[0] + box[2]) / 2 > cut for cut in cuts)].append(row)
    return [(lane, ["parallel_report_unplaced_text"] if unplaced else []) for lane in lanes if lane]


def segment_molecular_reports(blocks):
    pages = {}
    for block in blocks:
        pages.setdefault(block.document_page.page_number, []).append(block)
    reports, unknown = [], set()
    for page, rows in sorted(pages.items()):
        rows.sort(key=lambda r: (r.reading_order, str(r.pk)))
        page_reports = []
        for lane, limitations in _lanes(rows):
            current, prelude, before_title = None, [], True
            for text, pieces in logical_lines(lane):
                title = primary_title(text)
                if title:
                    if current:
                        page_reports.append(current)
                    current = MolecularSegment(title, list(prelude) if before_title else [], list(limitations))
                    before_title, prelude = False, []
                elif OTHER.search(matching_text(text)):
                    if current:
                        page_reports.append(current)
                    current, prelude, before_title = None, [], False
                elif current is None and before_title:
                    if METADATA.match(matching_text(text)):
                        prelude.extend(pieces)
                    elif text.strip():
                        prelude = []
                if current:
                    current.pieces.extend(pieces)
            if current:
                page_reports.append(current)
        header = "\n".join(r.text for r in rows[:5])
        identifiers = set(IDENTIFIER.findall(header))
        if CONTINUATION.search(header) and len(identifiers) == 1 and len(page_reports) <= 1 and not OTHER.search(header):
            previous = [r for r in reports if max(p.page for p in r.pieces) == page - 1
                        and set(IDENTIFIER.findall("\n".join(p.text for p in r.pieces))) == identifiers
                        and (not page_reports or r.title == page_reports[0].title)]
            if len(previous) == 1:
                previous[0].pieces.extend(p for row in rows for p in _lines(row))
                continue
        if page_reports:
            reports.extend(page_reports)
        else:
            unknown.add(page)
    return reports, unknown
