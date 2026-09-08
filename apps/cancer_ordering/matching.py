"""Bounded literal report candidates with original string offsets."""

from decimal import Decimal, InvalidOperation
import re
from types import MappingProxyType
import unicodedata

from .profiles import PROFILES
from .schema import Assertion, Subject


MATCHING_VERSION = "reported-cancer-literals-1"
ALIASES = MappingProxyType({
    "右肺上叶浸润性腺癌": "LUNG", "肺癌": "LUNG",
    "胰头导管腺癌": "PANCREAS", "胰腺癌": "PANCREAS", "胰头癌": "PANCREAS",
})
HEADINGS = {
    "DIAGNOSIS": ("出院诊断", "入院诊断", "临床诊断", "主要诊断", "初步诊断", "诊断"),
    "PATHOLOGY": ("病理检查结论", "病理诊断", "病理结论", "诊断结论", "诊断意见", "检查结论", "结论", "诊断"),
}
LITERAL = re.compile("|".join(re.escape(value) for value in sorted(ALIASES, key=len, reverse=True)))
MALIGNANCY_WORD = re.compile(r"癌|肉瘤|恶性肿瘤")
ORDINAL = re.compile(r"^(?:\(?[0-9IVX]+[.)、]|[一二三四五六七八九十]+、)")
LEFT_BOUNDARY = re.compile(
    r"(?:[、,/]|以及|合并|伴|及|和|与|或者|或|患者|"
    r"确诊为|诊断为|明确|提示|考虑|倾向|可能|可疑|疑似|"
    r"不能排除|无法排除|不能除外|不除外|待排|排除|未见|未发现|未检出|未证实|否认|无|"
    r"患有|曾患|患|既往|父亲|母亲|家族史[:：]?)$"
)
RIGHT_BOUNDARY = re.compile(
    r"^(?:$|[().、,/?]|p?[TNM][0-9xX?]|[IVX]+[A-C]?期|"
    r"明确|确诊|待排|待定|可能|可疑|倾向|考虑|不除外|不能除外|不能排除|无法排除|"
    r"未(?:见|检出|检测到|发现)|否认|术后|治疗后|合并|伴|及|和|与|或|转移)"
)
UNCERTAINTY = re.compile(r"不能(?:排除|除外)|无法(?:排除|除外)|不除外|待排|待定|考虑|可能|可疑|疑似|倾向|[?]|(?:^|癌)(?:或|或者|/)")
NEGATION = re.compile(r"未(?:见|发现|检出|检测到|证实|患)|否认|排除|(?:^|患者)无")
UNKNOWN_NEGATION = re.compile(r"未|不|无|否|难以")
OTHER_PERSON = re.compile(r"父亲|母亲|父母|兄弟|姐妹|祖父|祖母|外祖|家族史|他人")
HISTORY = re.compile(r"既往|曾患|病史|历史")
METASTATIC = re.compile(r"转移性|转移(?:癌|肿瘤)|继发(?:性)?(?:癌|肿瘤)")
CONTRAST = re.compile(r"但是|然而|不过|但|而")
COORDINATION = re.compile(r"以及|合并|伴|及|和|与|或|、|/")
CONTEXT_END = re.compile(CONTRAST.pattern + "|" + COORDINATION.pattern + r"|[,，]")


def _view(raw):
    text, positions = [], []
    for offset, char in enumerate(raw):
        for normalized in unicodedata.normalize("NFKC", char):
            if not normalized.isspace():
                text.append(normalized)
                positions.append(offset)
    return "".join(text), positions


def _body_start(text, positions, raw, category):
    names = "|".join(re.escape(name) for name in sorted(HEADINGS[category], key=len, reverse=True))
    heading = re.match(r"^[【\[]?(?P<name>" + names + r")[】\]]?", text)
    if heading is None or heading.end() == len(text):
        return None
    end = heading.end()
    if text[end] == ":":
        return end + 1, heading.group("name")
    # A separate source line is also an explicit heading. A mere prefix of a
    # longer word (e.g. 诊断性...) is not a section marker.
    gap = raw[positions[end - 1] + 1:positions[end]]
    if gap and gap.isspace():
        return end, heading.group("name")
    return None


def _clauses(text, start):
    for sentence in re.finditer(r"[^。;；]+", text[start:]):
        body = sentence.group()
        cuts = [0]
        for comma in re.finditer(r"[,，]", body):
            before, after = body[cuts[-1]:comma.start()], body[comma.end():]
            # Commas introducing a distinct explicit assertion can reset its
            # scope. Plain lists/qualifiers retain the preceding context.
            explicit = re.match(r"(?:但是|然而|但|另)?(?:未|否认|考虑|可能|不能|无法|不除外|诊断)", after)
            affirmed = any(after.startswith(alias + ending) for alias in ALIASES for ending in ("明确", "确诊"))
            if MALIGNANCY_WORD.search(before) and MALIGNANCY_WORD.search(after) and (explicit or affirmed):
                cuts.append(comma.end())
        cuts.append(len(body))
        for left, right in zip(cuts, cuts[1:]):
            while right > left and body[right - 1] in ",，":
                right -= 1
            if right > left:
                yield start + sentence.start() + left, start + sentence.start() + right


def _assertion(text, *, preliminary=False, disjunctive=False, ambiguous_scope=False):
    if ambiguous_scope:
        return Assertion.UNKNOWN.value
    if UNCERTAINTY.search(text) or preliminary or disjunctive:
        return Assertion.UNCERTAIN.value
    if NEGATION.search(text):
        return Assertion.NEGATED.value
    # Recognizing an inner word such as 患 does not prove the complete predicate
    # is affirmative. Unsupported negative constructions remain unresolved.
    if UNKNOWN_NEGATION.search(text):
        return Assertion.UNKNOWN.value
    return Assertion.AFFIRMED.value


def _subject(text):
    if OTHER_PERSON.search(text):
        return Subject.OTHER_PERSON.value
    if HISTORY.search(text):
        return Subject.HISTORICAL.value
    if METASTATIC.search(text):
        return Subject.METASTATIC_SITE.value
    return Subject.CURRENT_PRIMARY.value


def _context(body, start, end):
    prefix = body[:start]
    contrasts = list(CONTRAST.finditer(prefix))
    begin = contrasts[-1].end() if contrasts else 0
    tail = body[end:]
    boundary = CONTEXT_END.search(tail)
    finish = end + boundary.start() if boundary else len(body)
    comma = list(re.finditer(r"[,，]", body[begin:start]))
    # A comma alone cannot establish whether an earlier negative qualifier is
    # shared. Explicit new assertions have already been separated by _clauses.
    ambiguous_scope = bool(comma and _assertion(body[begin:begin + comma[-1].start()]) != Assertion.AFFIRMED.value)
    return body[begin:finish], ambiguous_scope


def _source_row(raw, positions, left, right, match_left, match_right, *, label, profile, context,
                preliminary=False, disjunctive=False, ambiguous_scope=False):
    start, end = positions[left], positions[right - 1] + 1
    match_start, match_end = positions[match_left], positions[match_right - 1] + 1
    return {"label": label, "profile": profile, "assertion": _assertion(context, preliminary=preliminary,
                disjunctive=disjunctive, ambiguous_scope=ambiguous_scope),
            "subject": _subject(context), "raw": raw[start:end], "start": start, "end": end,
            "label_raw": raw[match_start:match_end], "match_start": match_start, "match_end": match_end,
            "rule_version": MATCHING_VERSION, "limitations": [] if profile else ["unsupported_reported_diagnosis"]}


def literal_candidates(raw_text, category):
    """Recognize only declared report literals; retain unsupported cancer wording.

    This function supplies text ranges, not proof of their OCR/page origin. The
    persisted adapter must independently bind those ranges before automatic use.
    """
    if not isinstance(raw_text, str):
        raise ValueError("诊断来源必须是原始文字。")
    if category not in HEADINGS or not raw_text:
        return ()
    text, positions = _view(raw_text)
    start = _body_start(text, positions, raw_text, category)
    if start is None:
        return ()
    body_start, heading = start
    output = []
    for left, right in _clauses(text, body_start):
        body = text[left:right]
        if not MALIGNANCY_WORD.search(body):
            continue
        matches = []
        for match in LITERAL.finditer(body):
            prefix = ORDINAL.sub("", body[:match.start()])
            if (prefix and not LEFT_BOUNDARY.search(prefix)) or not RIGHT_BOUNDARY.match(body[match.end():]):
                continue
            matches.append(match)
            context, ambiguous_scope = _context(body, *match.span())
            output.append(_source_row(raw_text, positions, left, right, left + match.start(), left + match.end(),
                label=match.group(), profile=ALIASES[match.group()], context=context,
                preliminary=heading == "初步诊断", disjunctive=bool(re.search(r"或|/", body)),
                ambiguous_scope=ambiguous_scope))
        unrepresented = [word for word in MALIGNANCY_WORD.finditer(body)
                         if not any(match.start() <= word.start() and word.end() <= match.end() for match in matches)]
        if unrepresented:
            output.append(_source_row(raw_text, positions, left, right, left, right,
                label=raw_text[positions[left]:positions[right - 1] + 1], profile=None, context=body,
                preliminary=heading == "初步诊断"))
    return tuple(sorted(output, key=lambda item: (item["match_start"], item["match_end"])))


def eligible_for_auto(candidate, confidence_values, *, source_valid=False, reviewed=False):
    """Source confidence is never a probability of the report's medical meaning."""
    if (source_valid is not True or candidate.get("profile") not in PROFILES
            or candidate.get("profile") == "GENERAL" or candidate.get("assertion") != "AFFIRMED"
            or candidate.get("subject") != "CURRENT_PRIMARY"):
        return False
    if reviewed is True:
        return True
    values = tuple(confidence_values)
    if not values:
        return False
    try:
        return all(value is not None and not isinstance(value, bool) and (number := Decimal(str(value))).is_finite()
                   and Decimal("0.95") <= number <= 1 for value in values)
    except (InvalidOperation, ValueError, TypeError):
        return False
