"""Conservative report boundaries over original OCR Unicode strings."""

from dataclasses import dataclass, field
import re
import unicodedata

from apps.processing.value_objects import normalized_polygon, InvalidRegion


SEGMENTER_VERSION = "clinical-segments-v1"
TITLE = re.compile(r"(?:PET\s*[/／]?\s*CT|CT|MRI?|彩色超声|超声|磁共振|X线)(?:影像|检查|诊断)?报告(?:单|书)?", re.I)
OTHER_TITLE = re.compile(r"^(?:.{0,32}医院)?(?:入院记录|出院记录|出院小结|检验报告单|病理(?:诊断)?报告(?:单|书)?|基因检测报告)")
BODY_ANCHOR = re.compile(r"影像(?:表现|所见|描述|诊断|结论)|检查所见|超声(?:所见|提示)|诊断(?:意见|提示)")
REPORT_ID = re.compile(r"(?:检查号|报告号|申请单号|检查单号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})")
CONTINUATION = re.compile(r"续(?:上)?页|第\s*[2-9]\d*\s*页\s*[/／]\s*共\s*\d+\s*页")


@dataclass(frozen=True)
class Piece:
    block: object
    start: int
    end: int

    @property
    def text(self):
        return self.block.text[self.start:self.end]

    @property
    def page(self):
        return self.block.document_page.page_number


@dataclass
class Segment:
    title: str
    pieces: list = field(default_factory=list)
    limitations: list = field(default_factory=list)


def _box(block):
    try:
        polygon = normalized_polygon(getattr(block, "layout_polygon", None) or block.polygon)
    except InvalidRegion:
        return None
    if not polygon:
        return None
    return min(p[0] for p in polygon), min(p[1] for p in polygon), max(p[0] for p in polygon), max(p[1] for p in polygon)


def _title(line):
    # Match classification text with NFKC only. All saved offsets remain raw.
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
    match = TITLE.search(normalized)
    if match is None:
        return None
    before = normalized[:match.start()].strip()
    if before and not re.fullmatch(r"[^：。；]{1,40}(?:医院|中心|科)", before):
        return None
    after = normalized[match.end():].strip()
    if after and not re.match(r"(?:检查|姓名|患者|放射|影像|病人|报告|[：:])", after):
        return None
    return match.group()


def _lines(block):
    position = 0
    for line in block.text.splitlines(keepends=True):
        end = position + len(line)
        yield Piece(block, position, end)
        position = end


def logical_lines(blocks):
    """Use adjacent original boxes, retaining every individual raw source span."""
    if any(len(block.text.splitlines()) > 1 for block in blocks) or any(_box(block) is None for block in blocks):
        return [(piece.text, [piece]) for block in blocks for piece in _lines(block)]
    from .layout import source_lines
    return [(text, [Piece(block, 0, len(block.text)) for block in group]) for _, text, group in source_lines(blocks)]


def _lanes(blocks):
    anchors = []
    for text, pieces in logical_lines(blocks):
        if _title(text.strip()):
            boxes = [_box(piece.block) for piece in pieces]
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) if all(boxes) else None
            anchors.append((pieces[0].block, box))
    located = [(block, box) for block, box in anchors if box]
    # Parallel report headers establish lanes. Arbitrary whitespace never does.
    parallel = any(abs(a[1][1] - b[1][1]) < .08 and (a[1][2] < b[1][0] or b[1][2] < a[1][0])
                   for a in located for b in located if a[0].pk != b[0].pk)
    if not parallel:
        return [(blocks, [])]
    centers = sorted({(box[0] + box[2]) / 2 for _, box in located})
    cuts = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    groups = [[] for _ in centers]
    unplaced = []
    for block in blocks:
        box = _box(block)
        if box is None or any(box[0] < cut < box[2] for cut in cuts):
            unplaced.append(block)
            continue
        lane = sum((box[0] + box[2]) / 2 > cut for cut in cuts)
        groups[lane].append(block)
    return [(group, ["parallel_report_unplaced_text"] if unplaced else []) for group in groups if group]


def segment_reports(blocks):
    pages = {}
    for block in blocks:
        pages.setdefault(block.document_page.page_number, []).append(block)
    segments = []
    unparsed_pages = set()
    for page, page_blocks in sorted(pages.items()):
        page_blocks.sort(key=lambda b: (b.reading_order, str(b.pk)))
        raw_page = "\n".join(block.text for block in page_blocks)
        header = BODY_ANCHOR.split(unicodedata.normalize("NFKC", raw_page), maxsplit=1)[0]
        page_titles = [title for block in page_blocks for line in _lines(block) if (title := _title(line.text.strip()))]
        identifiers = set(REPORT_ID.findall(header))
        if CONTINUATION.search(header) and len(identifiers) == 1 and len(page_titles) <= 1 and not any(OTHER_TITLE.match(line.strip()) for line in raw_page.splitlines()):
            linked = [segment for segment in segments if max(piece.page for piece in segment.pieces) == page - 1
                      and set(REPORT_ID.findall("\n".join(piece.text for piece in segment.pieces))) == identifiers
                      and (not page_titles or page_titles[0] == segment.title)]
            if len(linked) == 1:
                linked[0].pieces.extend(piece for block in page_blocks for piece in _lines(block))
                continue
        found = False
        for lane, limits in _lanes(page_blocks):
            lane.sort(key=lambda b: (b.reading_order, str(b.pk)))
            current = None

            def finish():
                nonlocal current, found
                if current and BODY_ANCHOR.search("\n".join(p.text for p in current.pieces)):
                    segments.append(current)
                    found = True
                current = None

            for text, pieces in logical_lines(lane):
                line = text.strip()
                title = _title(line)
                if title:
                    finish()
                    current = Segment(title, [], list(limits))
                elif OTHER_TITLE.match(unicodedata.normalize("NFKC", line)):
                    finish()
                if current is not None:
                    current.pieces.extend(pieces)
            finish()
        if not found:
            # Unanchored continuation is not silently attached to another report.
            unparsed_pages.add(page)
    for segment in segments:
        merged = []
        for piece in segment.pieces:
            if merged and merged[-1].block.pk == piece.block.pk and merged[-1].end == piece.start:
                previous = merged.pop()
                merged.append(Piece(piece.block, previous.start, piece.end))
            else:
                merged.append(piece)
        segment.pieces = merged
    return segments, unparsed_pages


class ReportText:
    """A matching view with a reversible map to each untouched OCR character."""

    def __init__(self, pieces):
        self.pieces = pieces
        chars, offsets = [], []
        for index, piece in enumerate(pieces):
            for offset in range(piece.start, piece.end):
                for char in unicodedata.normalize("NFKC", piece.block.text[offset]):
                    if not char.isspace():
                        chars.append(char)
                        offsets.append((index, offset))
        self.text = "".join(chars)
        self.offsets = offsets

    def fragments(self, start, end):
        if not 0 <= start < end <= len(self.offsets):
            return []
        by_piece = {}
        for index, offset in self.offsets[start:end]:
            if index not in by_piece:
                by_piece[index] = [offset, offset + 1]
            else:
                by_piece[index][1] = offset + 1
        return [Piece(self.pieces[index].block, left, right) for index, (left, right) in by_piece.items()]

    def raw(self, start, end):
        return "\n".join(piece.text for piece in self.fragments(start, end))
