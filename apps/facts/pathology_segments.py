"""Bound primary pathology reports using explicit headings and original spans."""
from dataclasses import dataclass
import re
import unicodedata

from .clinical_segments import Piece, Segment, _box, _lines, logical_lines


SEGMENTER_VERSION = "pathology-segments-v1"
TITLE = re.compile(r"(?:病理(?:诊断)?|免疫组织化学|免疫组化|IHC)(?:检测|检查|诊断)?报告(?:单|书)?", re.I)
NAMED_ASSAY_TITLE = re.compile(r"(?:[A-Za-z0-9-]{2,30}(?:[(（][A-Za-z0-9-]{1,30}[)）])?(?:免疫组化|免疫组织化学|IHC)|(?:免疫组化|免疫组织化学)[(（][A-Za-z0-9-]{2,30}[)）])检测", re.I)
HEADER_METADATA = re.compile(r"^(?:标本编号|标本号|蜡块编号|组织块号|标本类型|标本名称|样本类型|样本编号|送检材料|采样日期|取材日期|收样日期|接收日期|报告日期)[:：]")
BODY = re.compile(r"(?:病理|组织学)诊断|检测结果|染色结果|免疫(?:组织化学|组化)结果|检测项目|抗体名称")
OTHER_REPORT = re.compile(r"^(?:[^：。；]{1,40}(?:医院|中心|科))?(?:CT|MR|超声|检验|基因检测|分子检测).{0,12}报告|^(?:入院记录|出院记录|出院小结)")
IDENTIFIER = re.compile(r"(?:病理号|报告号|报告编号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})")
CONTINUATION = re.compile(r"续(?:上)?页|第\s*[2-9]\d*\s*页\s*[/／]\s*共\s*\d+\s*页")


@dataclass
class PathologySegment(Segment):
    routing_kind: str = "PATHOLOGY"


def matching_text(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def primary_title(text):
    normalized = matching_text(text)
    if NAMED_ASSAY_TITLE.fullmatch(normalized):
        return normalized
    match = TITLE.search(normalized)
    if match is None:
        return None
    before, after = normalized[:match.start()], normalized[match.end():]
    if before and not re.fullmatch(r"[^：。；]{1,40}(?:医院|中心|科)", before):
        return None
    if after and not re.fullmatch(r"(?:[(（][^。；:：]{1,30}[)）])?", after):
        return None
    return match.group()


def report_lanes(blocks):
    # Evaluate each original heading before source_lines can join two reports.
    anchors = [(block, _box(block)) for block in blocks
               if len(block.text.splitlines()) == 1 and primary_title(block.text)]
    located = [(block, box) for block, box in anchors if box]
    parallel = any(a.pk != b.pk and abs(left[1] - right[1]) < .08
                   and (left[2] < right[0] or right[2] < left[0])
                   for a, left in located for b, right in located)
    if not parallel:
        return [(blocks, [])]
    centers = sorted({(box[0] + box[2]) / 2 for _, box in located})
    cuts = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    lanes = [[] for _ in centers]
    unplaced = False
    for block in blocks:
        box = _box(block)
        if box is None or any(box[0] < cut < box[2] for cut in cuts):
            unplaced = True
            continue
        lanes[sum((box[0] + box[2]) / 2 > cut for cut in cuts)].append(block)
    return [(lane, ["parallel_report_unplaced_text"] if unplaced else []) for lane in lanes if lane]


def segment_pathology_reports(blocks):
    pages = {}
    for block in blocks:
        pages.setdefault(block.document_page.page_number, []).append(block)
    reports, unknown = [], set()
    for page, rows in sorted(pages.items()):
        rows.sort(key=lambda row: (row.reading_order, str(row.pk)))
        page_reports = []
        for lane, limitations in report_lanes(rows):
            current = None
            prelude, before_first_report = [], True

            def finish():
                nonlocal current
                if current and BODY.search("\n".join(piece.text for piece in current.pieces)):
                    page_reports.append(current)
                current = None

            for text, pieces in logical_lines(lane):
                title = primary_title(text)
                if title:
                    finish()
                    current = PathologySegment(title, prelude if before_first_report else [], list(limitations))
                    prelude, before_first_report = [], False
                elif OTHER_REPORT.search(matching_text(text)):
                    finish()
                    prelude, before_first_report = [], False
                elif current is None and before_first_report:
                    if HEADER_METADATA.match(matching_text(text)):
                        prelude.extend(pieces)
                    elif BODY.search(text):
                        prelude, before_first_report = [], False
                if current is not None:
                    current.pieces.extend(pieces)
            finish()
        # Continuation requires the same explicit report identifier. A page
        # number or matching marker alone never joins separate specimens.
        header = BODY.split("\n".join(row.text for row in rows), maxsplit=1)[0]
        identifiers = set(IDENTIFIER.findall(header))
        if CONTINUATION.search(header) and len(identifiers) == 1 and len(page_reports) <= 1 and not OTHER_REPORT.search(header):
            previous = [report for report in reports if max(piece.page for piece in report.pieces) == page - 1
                        and set(IDENTIFIER.findall("\n".join(piece.text for piece in report.pieces))) == identifiers
                        and (not page_reports or report.title == page_reports[0].title)]
            if len(previous) == 1:
                previous[0].pieces.extend(piece for row in rows for piece in _lines(row))
                continue
        if page_reports:
            reports.extend(page_reports)
        else:
            unknown.add(page)
    return reports, unknown
