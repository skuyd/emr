from dataclasses import dataclass, field, replace
import hashlib
import re
import unicodedata

from apps.processing.value_objects import OcrPage, normalized_polygon
from tools.sample_dictionary.normalize import is_rejected_candidate_name, normalize_candidate_name

from .candidates import _rows, _union_polygon
from .dictionary import default_dictionary
from .models import CapabilityLevel, ResultType
from .quality import MIN_OBSERVATION_CONFIDENCE, MIN_STANDARD_NAME_CONFIDENCE


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
    if any(_result_type(item['after']) is not None for item in _normalization_candidates(raw, 'raw_value')):
        return raw, ResultType.STATUS, ()
    tokens = raw.split()
    if not tokens:
        return None
    first_type = _result_type(tokens[0])
    if first_type is not None:
        return tokens[0], first_type, tuple(tokens[1:])
    return None


def _unit_key(value):
    value = _normalized_unit(value).replace("µ", "μ")
    return re.sub(r"\s+", "", value).casefold()


def _normalized_unit(value):
    # NFKC alone turns 10⁹ into 109 and changes the exponent's meaning.
    value = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+', lambda match: '^' + unicodedata.normalize('NFKC', match.group()), value)
    return unicodedata.normalize('NFKC', value)


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
    specimen: str = ""
    field_evidence: dict = field(default_factory=dict)
    quality_issues: list = field(default_factory=list)
    normalization_candidates: list = field(default_factory=list)
    reference_range: dict = field(default_factory=dict)
    method_raw: str = ''

    def __post_init__(self):
        object.__setattr__(self, "region", normalized_polygon(self.region))
        if not self.raw_name or not self.standard_code or not self.standard_name:
            raise ValueError("Extracted observations require names and raw results")


def _candidate_identity(normalized_name, dictionary, *, specimen="", panel=""):
    indicator = dictionary.match(normalized_name, specimen=specimen, panel=panel)
    if indicator is None:
        # Printed name + abbreviation can be separate approved aliases for the same
        # item. Every fragment must independently agree; a recognizable prefix alone
        # cannot silently turn an unknown or conflicting label into a mapping.
        parts = [part for part in re.split(r'\s+|(?<=[A-Za-z0-9#%])(?=[\u3400-\u9fff])|(?<=[\u3400-\u9fff])(?=[A-Za-z])', normalized_name) if part]
        matches = [dictionary.match(part, specimen=specimen, panel=panel) for part in parts]
        if len(parts) > 1 and all(matches) and len({item.code for item in matches}) == 1:
            indicator = matches[0]
    if indicator is not None:
        return indicator, indicator.code, indicator.standard_name, indicator.capability_level.value
    digest = hashlib.sha256(normalized_name.casefold().encode("utf-8")).hexdigest()[:24].upper()
    return None, f"CANDIDATE_{digest}", normalized_name, CapabilityLevel.SEARCH_ONLY


def _flatten_tail(result_tail, remaining_regions):
    tokens = list(result_tail)
    for region in remaining_regions:
        tokens.extend(region.text.strip().split())
    return tuple(token for token in tokens if token)


def _field_source(page, regions):
    from .candidates import _bounds

    metadata = dict(page.provider_metadata)
    approximate = metadata.get("region_precision") == "page" or metadata.get("precision") == "page"
    approximate = approximate or any(
        (_bounds(region)[2] - _bounds(region)[0]) * (_bounds(region)[3] - _bounds(region)[1]) > .8
        for region in regions
    )
    if not regions or approximate:
        return {"page_number": page.page_number, "polygon": None, "precision": "page"}
    polygon = regions[0].polygon if len(regions) == 1 else _union_polygon(regions)
    return {"page_number": page.page_number, "polygon": [list(point) for point in polygon], "precision": "region"}


def _normalization_candidates(raw, field_name):
    candidates = []
    normalized = (_normalized_unit(raw) if field_name == 'raw_unit' else unicodedata.normalize("NFKC", raw)).replace("−", "-")
    if normalized != raw:
        candidates.append({"field": field_name, "before": raw, "after": normalized, "rule_version": "lab-normalization-v2", "reason": "字符宽度、上标或符号规范化，保留原文。"})
    if field_name == "raw_value":
        repaired = re.sub(r"(?<=\d),(?=\d)", ".", normalized)
        if re.fullmatch(r"[<>≤≥]?\s*[Oо][.]\d+", repaired):
            repaired = repaired.replace("O", "0").replace("о", "0")
        if repaired != normalized and _result_type(repaired) is not None:
            candidates.append({"field": field_name, "before": raw, "after": repaired, "rule_version": "lab-normalization-v2", "reason": "疑似小数点或数字混淆，候选需要人工核对。"})
    return candidates


def _extract_associated(association, dictionary, reading_order):
    from .layout import quality_issue
    from .validation import parse_reference_range

    row, page = association.regions, association.page
    source_row = row
    phase_two = any(item.specimen for item in dictionary.indicators)
    confidence = min(region.confidence for region in row)
    if confidence < float(MIN_OBSERVATION_CONFIDENCE) and not phase_two:
        return None
    fields = dict(association.fields)
    issues = list(association.issues)
    raw_unit = report_flag = reference_raw = method_raw = ""
    if not fields and len(row) == 1:
        tokens = row[0].text.split()
        if any(_result_type(token) for token in tokens[1:]):
            row = tuple(replace(row[0], text=token) for token in tokens)
    if fields:
        raw_name = " ".join(region.text.strip() for region in fields["raw_name"])
        raw_value = " ".join(region.text.strip() for region in fields.get("raw_value", ()))
        result_type = _result_type(raw_value)
        raw_unit = " ".join(region.text.strip() for region in fields.get("raw_unit", ()))
        reference_raw = " ".join(region.text.strip() for region in fields.get("reference_range_raw", ()))
        report_flag = " ".join(region.text.strip() for region in fields.get("report_flag_raw", ()))
        method_raw = " ".join(region.text.strip() for region in fields.get('method_raw', ()))
        flagged = re.fullmatch(r'(.*?)\s*(HH|LL|H|L|↑|↓)', raw_value, re.IGNORECASE)
        if flagged and _result_type(flagged.group(1).strip()) in {ResultType.NUMERIC, ResultType.COMPARATOR}:
            raw_value = flagged.group(1).strip()
            result_type = _result_type(raw_value)
            report_flag = ' '.join(part for part in (flagged.group(2), report_flag) if part)
            fields['report_flag_raw'] = (*fields.get('raw_value', ()), *fields.get('report_flag_raw', ()))
    else:
        parsed_result = None
        result_index = None
        consumed = 1
        for index, region in enumerate(row):
            if re.fullmatch(r'[<>≤≥]=?', _normalized_token(region.text)) and index + 1 < len(row) and _result_type(row[index + 1].text) == ResultType.NUMERIC:
                result_index, parsed_result = index, (' '.join(x.text for x in row[index:index + 2]), ResultType.COMPARATOR, ())
                consumed = 2
                break
            parsed = _result_and_tail(region.text)
            if parsed is not None:
                result_index, parsed_result = index, parsed
                break
        if result_index == 0:
            return None
        if result_index is None:
            indicator = dictionary.match(normalize_candidate_name(row[0].text), specimen=association.specimen, panel=association.panel)
            if not phase_two or indicator is None or not any(_is_unit(region.text, indicator) for region in row[1:]):
                return None
            # An identified item with a unit but no result remains an incomplete candidate.
            result_index = 1
            parsed_result = ('', ResultType.STATUS, ())
            fields['raw_value'] = ()
            issues.append(quality_issue('association_conflict', ['raw_value'], '项目缺少结果单元格，保留名称与单位候选。'))
        fields["raw_name"] = row[:result_index]
        raw_name = " ".join(region.text.strip() for region in fields["raw_name"])
        raw_value, result_type, result_tail = parsed_result
        fields.setdefault("raw_value", row[result_index:result_index + consumed])
        indicator = dictionary.match(normalize_candidate_name(raw_name), specimen=association.specimen, panel=association.panel)
        reference_parts = []
        tail = [(token, row[result_index]) for token in result_tail]
        tail_start = result_index + consumed if raw_value else result_index
        tail.extend((token, region) for region in row[tail_start:] for token in region.text.strip().split())
        for token, source in tail:
            if not report_flag and _FLAG.fullmatch(_normalized_token(token)):
                report_flag = token
                fields.setdefault("report_flag_raw", []).append(source)
            elif not raw_unit and _is_unit(token, indicator):
                raw_unit = token
                fields.setdefault("raw_unit", []).append(source)
            else:
                reference_parts.append(token)
                fields.setdefault("reference_range_raw", []).append(source)
                if phase_two and _result_type(token) in {ResultType.NUMERIC, ResultType.COMPARATOR}:
                    issues.append(quality_issue('association_conflict', ['raw_value', 'reference_range_raw'], '无表头行含多个数值候选，结果与参考范围的归属待核对。'))
        reference_raw = " ".join(reference_parts)
    normalized_name = normalize_candidate_name(raw_name)
    public_raw_name = normalize_candidate_name(raw_name, strip_result=False)
    single_known_name = bool(len(normalized_name) == 1 and len(public_raw_name) == 1
        and dictionary.match(normalized_name, specimen=association.specimen, panel=association.panel))
    if not normalized_name or ((is_rejected_candidate_name(normalized_name) or is_rejected_candidate_name(public_raw_name)) and not single_known_name):
        return None
    indicator, code, standard_name, capability = _candidate_identity(normalized_name, dictionary, specimen=association.specimen, panel=association.panel)
    project_regions = (*fields.get('project_code', ()), *fields.get('row_code', ()))
    if project_regions:
        project_name = normalize_candidate_name(' '.join(region.text for region in project_regions))
        combined_name = f'{project_name} {normalized_name}'
        project = dictionary.match(project_name, specimen=association.specimen, panel=association.panel)
        combined = dictionary.match(combined_name, specimen=association.specimen, panel=association.panel)
        if indicator is not None and project is not None and indicator.code != project.code:
            # A conflicting printed code is evidence to review, not authority
            # to replace the printed item name with a different indicator.
            indicator = None
            code = 'CANDIDATE_' + hashlib.sha256(combined_name.casefold().encode('utf-8')).hexdigest()[:24].upper()
            standard_name, capability = normalized_name, CapabilityLevel.SEARCH_ONLY
            issues.append(quality_issue('association_conflict', ['raw_name', 'standard_code'], '项目名称与同列代号指向不同项目，保留原文待核对。'))
        elif indicator is None and combined is not None:
            # Only an explicitly reviewed combined alias can disambiguate a
            # name such as white blood cells; no specimen is inferred here.
            indicator, code, standard_name, capability = _candidate_identity(combined_name, dictionary, specimen=association.specimen, panel=association.panel)
    if indicator is None and not raw_value and not raw_unit:
        return None
    if confidence < float(MIN_STANDARD_NAME_CONFIDENCE):
        standard_name = public_raw_name
    candidates = []
    for name, value in (("raw_value", raw_value), ("raw_unit", raw_unit)):
        candidates.extend(_normalization_candidates(value, name))
    if candidates:
        issues.append(quality_issue("normalization_uncertain", sorted({x["field"] for x in candidates}), "原文含规范化或修复候选，尚未替换原始字段。"))
    if result_type is None:
        result_type = ResultType.STATUS
        if not any(item["code"] == "association_conflict" for item in issues):
            issues.append(quality_issue("recognition_uncertain", ["raw_value"], "结果不能可靠归入已支持的值类型，保留原文待核对。"))
    if result_type in {ResultType.NUMERIC, ResultType.COMPARATOR} and not raw_unit and (indicator is None or indicator.unit_forms):
        if not phase_two:
            return None
        issues.append(quality_issue("unit_unknown", ["raw_unit"], "未识别到明确单位；没有从字典补写原报告单位。"))
    if phase_two and confidence < float(MIN_STANDARD_NAME_CONFIDENCE):
        issues.append(quality_issue("recognition_uncertain", ["raw_name", "raw_value"], "OCR 字段置信度不足，保留候选与来源。"))
    if phase_two and indicator is None:
        issues.append(quality_issue("mapping_unknown", ["raw_name"], "项目名称尚未唯一匹配字典，缺少上下文时不猜测标本或面板。"))
    if any(item["code"] in {"association_conflict", "recognition_uncertain", "normalization_uncertain", "unit_unknown"} for item in issues):
        capability = CapabilityLevel.SEARCH_ONLY
    evidence = {name: _field_source(page, tuple(dict.fromkeys(fields.get(name, ())))) for name in (
        'raw_name', 'raw_value', 'raw_unit', 'reference_range_raw', 'report_flag_raw', 'method_raw',
    )}
    if project_regions:
        evidence['standard_code'] = _field_source(page, (*project_regions, *fields['raw_name']))
    if association.specimen_source:
        source_page, source_region = association.specimen_source
        evidence["specimen"] = _field_source(source_page, (source_region,))
    return ExtractedObservation(
        page_number=page.page_number, reading_order=reading_order,
        raw_name=raw_name if phase_two else public_raw_name, standard_code=code, standard_name=standard_name,
        raw_value=raw_value, result_type=result_type, raw_unit=raw_unit,
        reference_range_raw=reference_raw, report_flag_raw=report_flag, capability_level=capability,
        dictionary_version=dictionary.version, region=_union_polygon(row), confidence=confidence,
        source_text=" ".join(region.text.strip() for region in source_row), specimen=association.specimen,
        field_evidence=evidence, quality_issues=issues, normalization_candidates=candidates,
        reference_range=parse_reference_range(reference_raw),
        method_raw=method_raw,
    )


def extract_observations(pages, dictionary=None):
    from .layout import associated_rows

    dictionary = dictionary or default_dictionary()
    observations = []
    orders = {}
    for association in associated_rows(pages, dictionary):
        page_number = association.page.page_number
        extracted = _extract_associated(association, dictionary, orders.get(page_number, 0) + 1)
        if extracted is not None:
            orders[page_number] = extracted.reading_order
            observations.append(extracted)
    return tuple(observations)
