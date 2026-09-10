"""Bind assertions to uncropped source statements, not typed substrings."""
import re
import unicodedata

from django.core.exceptions import ValidationError

from .molecular_source_codes import assertion_codes


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def statement_windows(text, *, start=0, end=None):
    """Only strong statement punctuation delimits a window; wraps/colons do not."""
    end = len(text) if end is None else end
    for match in re.finditer(r"[^;；。]+", text):
        if match.start() < end and match.end() > start:
            yield match.group()


def supports_original_statement(code, raw, windows):
    needle = normalized(raw)
    matches = [window for window in windows if needle in normalized(window)]
    return bool(matches) and all(assertion_codes(window) == {code} for window in matches)


def continuous_ranges(text, intervals):
    """OCR line spans separated only by whitespace are one original scope."""
    merged = []
    for start, end in sorted(intervals):
        if merged and (start <= merged[-1][1] or not text[merged[-1][1]:start].strip()):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def validate_original_assertion(fact, code, raw, pieces):
    if fact.origin == "MANUAL":
        # Separate transcribed fragments are not evidence of separate clauses.
        # Preserve every original modifier before selecting a whole statement.
        text = "\n".join(piece.raw_text for piece in sorted(pieces, key=lambda p: p.ordinal))
        # A declared count is not a linguistic boundary. Where a selected
        # statement reaches the last own fragment, later copied fragments must
        # not be used to hide a modifier of that same unfinished statement.
        tail = re.split(r"[;；。]", text)[-1]
        if len(pieces) < fact.source_fragments.count() and normalized(raw) in normalized(tail):
            raise ValidationError("断言在首组末尾没有明确原句边界，不能用片段计数将其修饰移入关联依据。")
        if not supports_original_statement(code, raw, statement_windows(text)):
            raise ValidationError("完整人工原文不能支持此断言；不能按片段丢弃前后修饰。")
        return
    windows = []
    for piece in pieces:
        if normalized(raw) not in normalized(piece.raw_text):
            continue
        if fact.origin == "AUTOMATIC":
            # Expand an actual offset back to its original OCR statement. A
            # value declaration alone must not cut off preceding negation.
            if piece.ocr_block_id is None:
                raise ValidationError("自动断言须保留可核验的实际 OCR 窗口。")
            text = piece.ocr_block.text
            spans = [(start, end) for start, end in continuous_ranges(text,
                [(s.start_offset, s.end_offset) for s in fact.clinical_report.spans.all() if s.ocr_block_id == piece.ocr_block_id])
                if start <= piece.start_offset and end >= piece.end_offset]
            if len(spans) != 1:
                raise ValidationError("断言必须能唯一定位在自己报告的实际原文跨度内。")
            start, end = spans[0]
            source = text[start:end]
            windows.extend(statement_windows(source, start=piece.start_offset - start, end=piece.end_offset - start))
        else:
            windows.extend(statement_windows(piece.raw_text))
    if not supports_original_statement(code, raw, windows):
        raise ValidationError("完整原文陈述不能支持此断言；不能截去否定、换行或未判断的修饰。")
