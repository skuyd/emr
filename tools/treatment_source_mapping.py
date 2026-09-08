"""Auditable literal alignment to frozen OCR; this module never reads gold values.

Only Unicode whitespace is removed for location matching. OCR region numbers are
one-based positions in stable reading_order, matching the original annotation
convention. Returned start/end offsets address the original region's Unicode
characters. Fact-local offsets are valid only inside their exact full source.
"""
import hashlib
import json
from pathlib import Path


MAPPING_VERSION = "literal-ocr-source-mapping-1"


def compact(text):
    return "".join(character for character in text if not character.isspace())


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _matches(text, needle):
    if not needle:
        return []
    result, offset = [], 0
    while (index := text.find(needle, offset)) >= 0:
        result.append(index)
        offset = index + 1
    return result


def _spans(coordinates):
    result = []
    for region, position in coordinates:
        if result and result[-1]["region"] == region:
            result[-1]["end"] = position + 1
        else:
            result.append({"region": region, "start": position, "end": position + 1})
    return result


class SourceMapper:
    def __init__(self, pages):
        self.pages = {}
        for key, regions in pages.items():
            ordered = sorted(regions, key=lambda row: row.get("reading_order", 0))
            self.pages[key] = [(number, row["text"]) for number, row in enumerate(ordered, 1)]

    @classmethod
    def from_manifest(cls, manifest):
        pages = {}
        for source in manifest["sources"]:
            path = Path(source["ocr_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["ocr_sha256"]:
                raise ValueError("Frozen OCR identity differs")
            cached = json.loads(path.read_text(encoding="utf-8"))
            for page in cached["pages"]:
                key = (source["source_number"], page["page_number"])
                if key in pages:
                    raise ValueError("Duplicate frozen OCR page")
                pages[key] = page["regions"]
        return cls(pages)

    def map_quote(self, source_number, page, raw, *, context=None, start=None, end=None, regions=None):
        receipt = {"version": MAPPING_VERSION, "normalization": "unicode-whitespace-only",
            "source_number": source_number, "page": page, "raw_text_sha256": text_hash(raw),
            "context_sha256": text_hash(context) if context is not None else None,
            "local_start": start, "local_end": end, "coordinate_system": "original_ocr_region_unicode",
            "status": "UNJUDGED", "reason": "", "match_count": 0, "context_match_count": None,
            "region_numbers": [], "spans": []}

        def reject(reason):
            receipt["reason"] = reason
            return receipt

        rows = self.pages.get((source_number, page))
        if rows is None:
            return reject("source_page_missing")
        if regions is not None:
            if not regions or any(type(number) is not int or number < 1 or number > len(rows) for number in regions):
                return reject("gold_region_locator_invalid")
            rows = [row for row in rows if row[0] in set(regions)]
        characters, coordinates = [], []
        for number, text in rows:
            for position, character in enumerate(text):
                if not character.isspace():
                    characters.append(character)
                    coordinates.append((number, position))
        haystack, needle = "".join(characters), compact(raw)
        matches = _matches(haystack, needle)
        receipt["match_count"] = len(matches)
        if not needle:
            return reject("source_quote_missing")
        if context is not None:
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(context)
                    or context[start:end] != raw):
                return reject("source_slice_mismatch")
            context_matches = _matches(haystack, compact(context))
            receipt["context_match_count"] = len(context_matches)
            if len(context_matches) == 1:
                before = len(compact(context[:start]))
                position = context_matches[0] + before
            elif len(matches) == 1:
                # The full extraction may omit a layout separator. An exact,
                # unique quoted claim still proves its own original position.
                position = matches[0]
            else:
                return reject("ambiguous_quote" if matches else "quote_not_found")
        elif len(matches) == 1:
            position = matches[0]
        else:
            return reject("ambiguous_quote" if matches else "quote_not_found")
        spans = _spans(coordinates[position:position + len(needle)])
        receipt.update(status="MATCHED", reason="unique_source_context" if receipt["context_match_count"] == 1 else "unique_literal_quote",
                       region_numbers=list(dict.fromkeys(row["region"] for row in spans)), spans=spans)
        return receipt

    def map_proof(self, proof, *, gold=False):
        if gold:
            return self.map_quote(proof["source_number"], proof["page"], proof.get("text", ""),
                                  regions=proof.get("region_numbers") or None)
        return self.map_quote(proof["source_number"], proof["page"], proof.get("raw_text", proof.get("raw", "")),
                              context=proof.get("source_context"), start=proof.get("start_offset"), end=proof.get("end_offset"))


def spans_overlap(left, right):
    return any(a["region"] == b["region"] and max(a["start"], b["start"]) < min(a["end"], b["end"])
               for a in left for b in right)
