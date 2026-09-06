"""Join adjacent source boxes into lines without inventing an excerpt region."""
from itertools import groupby

from apps.processing.value_objects import InvalidRegion, normalized_polygon


def source_lines(blocks):
    for page_id, page_blocks in groupby(blocks, key=lambda block: block.document_page_id):
        page_blocks = list(page_blocks)
        boxes = []
        try:
            for block in page_blocks:
                if len(block.text.splitlines()) > 1:
                    raise InvalidRegion("Multiline blocks keep their provider reading order")
                polygon = normalized_polygon(block.polygon)
                x, y = zip(*polygon)
                boxes.append((min(x), min(y), max(x), max(y), block))
        except (InvalidRegion, TypeError):
            for block in page_blocks:
                for text in block.text.splitlines():
                    yield page_id, text, [block]
            continue
        rows = []
        for box in sorted(boxes, key=lambda value: ((value[1]+value[3])/2, value[0])):
            height = box[3]-box[1]
            # Baseline clustering is bounded to recent rows, avoiding quadratic scans on character PDFs.
            row = next((row for row in reversed(rows[-5:]) if (
                min(row["bottom"], box[3])-max(row["top"], box[1]) >= .6*min(row["bottom"]-row["top"],height)
            )), None)
            if row is None:
                row = {"top": box[1], "bottom": box[3], "boxes": []}
                rows.append(row)
            row["boxes"].append(box)
        for row in rows:
            pending = None
            for box in sorted(row["boxes"], key=lambda value: value[0]):
                text = box[4].text.strip()
                if not text:
                    continue
                if pending:
                    previous = pending["last"]
                    width = max((previous[2]-previous[0])/max(len(previous[4].text),1),
                                (box[2]-box[0])/max(len(text),1))
                    gap = box[0]-previous[2]
                    if -.1*width <= gap <= 1.6*width:
                        separator = "" if len(previous[4].text)==1 or len(text)==1 else " "
                        pending["text"] += separator+text
                        pending["blocks"].append(box[4])
                        pending["last"] = box
                        continue
                    yield page_id, pending["text"], pending["blocks"]
                pending = {"text": text, "blocks": [box[4]], "last": box}
            if pending:
                yield page_id, pending["text"], pending["blocks"]
