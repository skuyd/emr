from dataclasses import dataclass
import hashlib
import re
import unicodedata

from apps.processing.value_objects import OcrPage, normalized_polygon
from tools.sample_dictionary.normalize import is_rejected_candidate_name, normalize_candidate_name

from .candidates import _rows, _union_polygon
from .dictionary import default_dictionary
from .models import CapabilityLevel, ResultType


_NUMERIC = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$")
_COMPARATOR = re.compile(r"^(?:[<>≤≥]=?)\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$")
_SEMI_QUANTITATIVE = re.compile(r"^(?:\+{1,4}|-{1,4}|±|\d\+)$")
_QUALITATIVE = frozenset({"阴性", "阳性", "弱阳性", "可疑", "未见", "正常", "异常", "negative", "positive"})
_STATUS = frozenset({"未检出", "未报告", "溶血", "拒收"})
_FLAG = re.compile(r"^(?:H|L|HH|LL|↑|↓|\*|异常)$", re.IGNORECASE)
_GENERIC_UNIT = re.compile(r"^[A-Za-z0-9μµu％%个秒][A-Za-z0-9μµu％%个秒^*/.()_-]{0,31}$", re.IGNORECASE)


def _normalized_token(value):
    return unicodedata.normalize("NFKC", value).strip()


def _result_type(value):
    normalized = _normalized_token(value)
    lowered = normalized.casefold()
    if _COMPARATOR.fullmatch(normalized):
        return ResultType.COMPARATOR
    if _NUMERIC.fullmatch(normalized):
        return ResultType.NUMERIC
    if _SEMI_QUANTITATIVE.fullmatch(normalized):
        return ResultType.SEMI_QUANTITATIVE
    if lowered in {item.casefold() for item in _STATUS}:
        return ResultType.STATUS
    if lowered in {item.casefold() for item in _QUALITATIVE}:
        return ResultType.QUALITATIVE
    return None


def _result_and_tail(value):
    raw = value.strip()
    if not raw:
        return None
    direct = _result_type(raw)
    if direct is not None:
        return raw, direct, ()
    tokens = raw.split()
    if not tokens:
        return None
    first_type = _result_type(tokens[0])
    if first_type is not None:
        return tokens[0], first_type, tuple(tokens[1:])
    return None


def _unit_key(value):
    value = unicodedata.normalize("NFKC", value).replace("µ", "μ")
    return re.sub(r"\s+", "", value).casefold()


def _is_unit(value, indicator):
    key = _unit_key(value)
    if not key:
        return False
    if indicator is not None and key in {_unit_key(item) for item in indicator.unit_forms}:
        return True
    return _GENERIC_UNIT.fullmatch(unicodedata.normalize("NFKC", value).strip()) is not None and (
        "/" in value or "%" in value or "％" in value or value.casefold() in {"s", "秒", "fl", "pg"}
    )


@dataclass(frozen=True)
class ExtractedObservation:
    page_number: int
    reading_order: int
    raw_name: str
    standard_code: str
    standard_name: str
    raw_value: str
    result_type: str
    raw_unit: str
    reference_range_raw: str
    report_flag_raw: str
    capability_level: str
    dictionary_version: str
    region: tuple[tuple[float, float], ...]
    confidence: float
    source_text: str

    def __post_init__(self):
        object.__setattr__(self, "region", normalized_polygon(self.region))
        if not self.raw_name or not self.raw_value or not self.standard_code or not self.standard_name:
            raise ValueError("Extracted observations require names and raw results")


def _candidate_identity(normalized_name, dictionary):
    indicator = dictionary.match(normalized_name)
    if indicator is not None:
        return indicator, indicator.code, indicator.standard_name, indicator.capability_level.value
    digest = hashlib.sha256(normalized_name.casefold().encode("utf-8")).hexdigest()[:24].upper()
    return None, f"CANDIDATE_{digest}", normalized_name, CapabilityLevel.SEARCH_ONLY


def _flatten_tail(result_tail, remaining_regions):
    tokens = list(result_tail)
    for region in remaining_regions:
        tokens.extend(region.text.strip().split())
    return tuple(token for token in tokens if token)


def _extract_row(row, dictionary, page_number, reading_order):
    result_index = None
    parsed_result = None
    for index, region in enumerate(row):
        parsed = _result_and_tail(region.text)
        if parsed is not None:
            result_index, parsed_result = index, parsed
            break
    if result_index in (None, 0):
        return None
    name_regions = row[:result_index]
    raw_name = " ".join(region.text.strip() for region in name_regions).strip()
    normalized_name = normalize_candidate_name(raw_name)
    public_raw_name = normalize_candidate_name(raw_name, strip_result=False)
    if (
        not normalized_name
        or is_rejected_candidate_name(normalized_name)
        or is_rejected_candidate_name(public_raw_name)
    ):
        return None
    indicator, standard_code, standard_name, capability = _candidate_identity(normalized_name, dictionary)
    raw_value, result_type, result_tail = parsed_result
    raw_unit = ""
    report_flag = ""
    reference_parts = []
    for token in _flatten_tail(result_tail, row[result_index + 1 :]):
        if not report_flag and _FLAG.fullmatch(_normalized_token(token)):
            report_flag = token.strip()
        elif not raw_unit and _is_unit(token, indicator):
            raw_unit = token.strip()
        else:
            reference_parts.append(token.strip())
    if result_type in {ResultType.NUMERIC, ResultType.COMPARATOR}:
        if indicator is None and not raw_unit:
            return None
        if indicator is not None and indicator.unit_forms and not raw_unit:
            return None
    evidence_regions = row
    source_text = " ".join(region.text.strip() for region in row).strip()
    return ExtractedObservation(
        page_number=page_number,
        reading_order=reading_order,
        raw_name=public_raw_name,
        standard_code=standard_code,
        standard_name=standard_name,
        raw_value=raw_value,
        result_type=result_type,
        raw_unit=raw_unit,
        reference_range_raw=" ".join(reference_parts),
        report_flag_raw=report_flag,
        capability_level=capability,
        dictionary_version=dictionary.version,
        region=_union_polygon(evidence_regions),
        confidence=min(region.confidence for region in evidence_regions),
        source_text=source_text,
    )


def extract_observations(pages, dictionary=None):
    dictionary = dictionary or default_dictionary()
    observations = []
    for page in pages:
        if not isinstance(page, OcrPage):
            raise ValueError("Observation extraction requires OCR pages")
        page_order = 0
        for row in _rows(page):
            extracted = _extract_row(row, dictionary, page.page_number, page_order + 1)
            if extracted is not None:
                page_order += 1
                observations.append(extracted)
    return tuple(observations)
