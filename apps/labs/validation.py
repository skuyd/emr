"""Explain transcription uncertainty without deriving medical risk or default reference ranges."""

from decimal import Decimal, InvalidOperation
import re
import unicodedata

from apps.processing.models import DatePrecision, MetadataKind

from .dictionary import DictionaryError, dictionary_for_version, rules_for_version
from .extraction import _result_type, _unit_key
from .models import ResultType
from .numerics import NUMBER, calculate_numeric, numeric_value
from .quality import MIN_TREND_CONFIDENCE, QUALITY_POLICY_VERSION


VALIDATION_RULE_VERSION = "lab-transcription-v2"
REVIEWABLE_ISSUES = frozenset({"recognition_uncertain", "association_conflict", "normalization_uncertain", "magnitude_suspect"})
ISSUE_LABELS = {
    "recognition_uncertain": "识别不确定", "source_unavailable": "来源不可用",
    "association_conflict": "字段关联冲突", "unit_unknown": "单位不明",
    "reference_unknown": "参考范围不明", "reference_conflict": "参考范围冲突",
    "date_uncertain": "日期依据不足", "date_conflict": "日期冲突",
    "type_conflict": "结果类型与内容不符", "mapping_unknown": "项目尚未核实",
    "magnitude_suspect": "量级疑似识别错误", "internal_conflict": "报告内部不一致",
    "normalization_uncertain": "文字修复待核对", "reported_error": "已反馈识别有误",
    "revision_conflict": "新解析与人工修订存在冲突", "specimen_unknown": "标本依据不足",
    "specimen_conflict": "标本与项目定义不符",
    "source_policy_unknown": "来源质量依据尚未验证",
    "numeric_unsupported": "数值超出可计算范围，请核对原文",
}
TREND_BLOCKING_ISSUES = frozenset(ISSUE_LABELS) - {"reference_unknown", "reference_conflict"}
REFERENCE_BLOCKING_ISSUES = frozenset(ISSUE_LABELS) - {"date_uncertain", "date_conflict", "specimen_unknown"}


def parse_reference_range(raw):
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    text = text.replace("−", "-").replace("～", "~").replace("至", "~")
    if not text:
        return {"kind": "unknown", "reason": "reference_unknown"}
    pair = re.fullmatch(rf"\s*({NUMBER})\s*[-~—–]\s*({NUMBER})\s*", text)
    if pair:
        low, high = (numeric_value(value) for value in pair.groups())
        if low is not None and high is not None and low <= high:
            return {"kind": "numeric", "low": str(low), "high": str(high), "low_inclusive": True, "high_inclusive": True}
        return {"kind": "unknown", "reason": "reference_conflict"}
    single = re.fullmatch(rf"([<>]=?|≤|≥)\s*({NUMBER})", text)
    if single:
        operator, value = single.groups()
        if numeric_value(value) is None:
            return {"kind": "unknown", "reason": "reference_conflict"}
        lower = operator.startswith(">") or operator == "≥"
        inclusive = "=" in operator or operator in {"≤", "≥"}
        return {"kind": "numeric", "low": value if lower else None, "high": None if lower else value,
                "low_inclusive": inclusive if lower else False, "high_inclusive": inclusive if not lower else False}
    values = [part.strip() for part in re.split(r"[,，、/或]", text) if part.strip()]
    if values and all(_result_type(value) in {ResultType.QUALITATIVE, ResultType.SEMI_QUANTITATIVE, ResultType.STATUS} for value in values):
        return {"kind": "enumeration", "values": values}
    return {"kind": "unknown", "reason": "reference_unknown"}


def issue(code, fields, *, details="", rule_version=VALIDATION_RULE_VERSION, rule_id=""):
    return {"code": code, "label": ISSUE_LABELS.get(code, "待核对"), "fields": list(fields),
            "rule_version": rule_version, "rule_id": rule_id or code, "details": details}


def _decimal_confidence(evidence):
    try:
        return Decimal(str(evidence.confidence))
    except (AttributeError, InvalidOperation):
        return Decimal("-1")


def _date_issues(observation):
    if observation.observation_date is None:
        return [issue("date_uncertain", ["observation_date"])]
    if getattr(observation, "date_verified", False):
        return []
    version = observation.parsing_version
    date_evidence = observation.field_evidence.get('observation_date', {})
    source = getattr(observation, "value_sources", {}).get("observation_date", {})
    if source.get("observation_id") and source["observation_id"] != str(observation.pk):
        from .models import LabObservation
        original = LabObservation.objects.select_related("parsing_version").filter(
            pk=source["observation_id"], parsing_version__document_id=version.document_id,
        ).first()
        if original is None:
            return [issue("date_uncertain", ["observation_date"])]
        version = original.parsing_version
        date_evidence = original.field_evidence.get('observation_date', {})
    candidates = version.metadata_candidates.filter(kind=MetadataKind.DOCUMENT_DATE, selected=True).select_related("evidence")
    if date_evidence.get('page_number'):
        candidates = candidates.filter(evidence__document_page__page_number=date_evidence['page_number'])
    reliable = [candidate for candidate in candidates if candidate.precision == DatePrecision.DAY
                and candidate.confidence >= MIN_TREND_CONFIDENCE
                and _decimal_confidence(candidate.evidence) >= MIN_TREND_CONFIDENCE]
    if not reliable:
        return [issue("date_uncertain", ["observation_date"])]
    values = {candidate.normalized_value for candidate in reliable}
    if values != {observation.observation_date.isoformat()}:
        return [issue("date_conflict", ["observation_date"])]
    return []


def _history_issues(observation, previous, rules, dictionary):
    output = []
    def previous_issues(item):
        item_version = getattr(item, 'mapping_dictionary_version', item.dictionary_version)
        if dictionary is not None and item_version == dictionary.version:
            return validate_observation(item, dictionary=dictionary, rules=rules)
        return validate_observation(item)
    current_value = numeric_value(observation.raw_value) if observation.result_type == ResultType.NUMERIC else None
    if current_value is None or not observation.observation_date or not observation.specimen or not observation.method_raw:
        return output
    for rule in rules:
        if not all(rule.get(name) for name in ("id", "version", "reviewed_by", "rationale", "code", "specimen", "method", "unit")):
            continue
        if rule.get("kind") != "history_ratio" or (
            rule["code"] != observation.standard_code or rule["specimen"] != observation.specimen
            or rule["method"] != observation.method_raw or _unit_key(rule["unit"]) != _unit_key(observation.raw_unit)
        ):
            continue
        threshold = numeric_value(rule.get("minimum_ratio"))
        if threshold is None or threshold <= 1:
            continue
        comparable = [item for item in previous if item.standard_code == observation.standard_code
                      and item.specimen == observation.specimen and item.method_raw == observation.method_raw
                      and _unit_key(item.raw_unit) == _unit_key(observation.raw_unit)
                      and item.result_type == ResultType.NUMERIC and item.observation_date
                      and item.observation_date < observation.observation_date
                      and item.parsing_version.document.patient_id == observation.parsing_version.document.patient_id
                      and not {x["code"] for x in previous_issues(item)} & TREND_BLOCKING_ISSUES]
        if not comparable:
            continue
        latest_date = max(item.observation_date for item in comparable)
        latest = [item for item in comparable if item.observation_date == latest_date]
        # Multiple exams are retained, but an ambiguous baseline cannot drive a change rule.
        if len(latest) != 1:
            continue
        previous_value = numeric_value(latest[0].raw_value)
        if previous_value is None or previous_value <= 0 or current_value <= 0:
            continue
        ratio = calculate_numeric(lambda: max(current_value / previous_value, previous_value / current_value))
        if ratio is None:
            output.append(issue('numeric_unsupported', ['raw_value'], rule_version=rule['version'], rule_id=rule['id']))
            continue
        if ratio >= threshold:
            output.append(issue("magnitude_suspect", ["raw_value"], rule_version=rule["version"],
                                rule_id=rule["id"], details="满足已审核规则的同项目、标本、方法与单位历史差异；请核对转录。"))
    return output


def _internal_issues(observation, peers, rules, dictionary):
    output = []
    def component_issues(item):
        item_version = getattr(item, 'mapping_dictionary_version', item.dictionary_version)
        snapshot = dictionary if dictionary is not None and item_version == dictionary.version else None
        return validate_observation(item, dictionary=snapshot, rules=())
    current_value = numeric_value(observation.raw_value) if observation.result_type == ResultType.NUMERIC else None
    if current_value is None:
        return output
    for rule in rules:
        if rule.get("kind") != "report_sum" or not all(rule.get(name) for name in (
            "id", "version", "reviewed_by", "rationale", "code", "component_codes", "specimen", "method", "unit",
        )):
            continue
        if (rule["code"] != observation.standard_code or rule["specimen"] != observation.specimen
                or rule["method"] != observation.method_raw or _unit_key(rule["unit"]) != _unit_key(observation.raw_unit)):
            continue
        tolerance = numeric_value(rule.get("absolute_tolerance"))
        codes = rule["component_codes"]
        if tolerance is None or tolerance < 0 or not isinstance(codes, list) or len(codes) != len(set(codes)):
            continue
        values = []
        for code in codes:
            matches = [item for item in peers if item.standard_code == code and item.pk != observation.pk
                       and item.parsing_version_id == observation.parsing_version_id
                       and item.specimen == observation.specimen and item.method_raw == observation.method_raw
                       and _unit_key(item.raw_unit) == _unit_key(observation.raw_unit)
                       and item.result_type == ResultType.NUMERIC
                       and not {entry['code'] for entry in component_issues(item)} & TREND_BLOCKING_ISSUES]
            if len(matches) != 1 or numeric_value(matches[0].raw_value) is None:
                break
            values.append(numeric_value(matches[0].raw_value))
        if len(values) != len(codes):
            continue
        difference = calculate_numeric(lambda: abs(sum(values) - current_value))
        if difference is None:
            output.append(issue('numeric_unsupported', ['raw_value'], rule_version=rule['version'], rule_id=rule['id']))
        elif difference > tolerance:
            output.append(issue("internal_conflict", ["raw_value"], rule_version=rule["version"],
                                rule_id=rule["id"], details="报告内部字段不满足已审核的一致性规则，请核对原件。"))
    return output


def validate_observation(observation, *, previous=(), dictionary=None, rules=None):
    version = getattr(observation, "mapping_dictionary_version", observation.dictionary_version)
    if dictionary is None:
        try:
            dictionary = dictionary_for_version(version)
        except DictionaryError:
            dictionary = None
    if rules is None:
        rules = rules_for_version(version) if dictionary is not None else ()
    # Mapping and unit availability are derived from the effective fields below.
    # Keep immutable OCR/layout issues, but do not perpetuate an obsolete gate
    # after an audited correction has supplied the missing field.
    issues = [dict(item) for item in observation.quality_issues if item.get('code') not in {'mapping_unknown', 'unit_unknown'}]
    resolved = set(getattr(observation, "resolved_issues", ())) & REVIEWABLE_ISSUES
    if observation.parsing_version.document.deleted_at is not None:
        issues.append(issue("source_unavailable", ["raw_name", "raw_value", "raw_unit"]))
    if observation.parsing_version.diagnostics.get('quality_policy') != QUALITY_POLICY_VERSION:
        issues.append(issue('source_policy_unknown', ['raw_name', 'raw_value', 'raw_unit']))
    confidence = _decimal_confidence(observation.evidence)
    if confidence < 0:
        issues.append(issue("source_unavailable", ["raw_name", "raw_value", "raw_unit"]))
    elif confidence < MIN_TREND_CONFIDENCE:
        issues.append(issue("recognition_uncertain", ["raw_name", "raw_value", "raw_unit"]))
    # Each retained field keeps its original OCR evidence after a reparse/status edit.
    sources = getattr(observation, "value_sources", {})
    if sources:
        from apps.processing.models import SourceEvidence
        evidence = {str(item.pk): item for item in SourceEvidence.objects.select_related('parsing_version').filter(
            pk__in=[item["evidence_id"] for item in sources.values()],
            parsing_version__document_id=observation.parsing_version.document_id,
        )}
        for field in ("raw_name", "raw_value", "raw_unit", "reference_range_raw"):
            if not getattr(observation, field):
                continue
            source_confidence = _decimal_confidence(evidence.get(sources.get(field, {}).get("evidence_id")))
            source = evidence.get(sources.get(field, {}).get('evidence_id'))
            if source is not None and source.parsing_version.diagnostics.get('quality_policy') != QUALITY_POLICY_VERSION:
                issues.append(issue('source_policy_unknown', [field]))
            if source_confidence < 0:
                issues.append(issue("source_unavailable", [field]))
            elif source_confidence < MIN_TREND_CONFIDENCE:
                issues.append(issue("recognition_uncertain", [field]))
    definition = next((item for item in dictionary.indicators if item.code == observation.standard_code), None) if dictionary else None
    if definition is None or observation.standard_code.startswith("CANDIDATE_"):
        issues.append(issue("mapping_unknown", ["raw_name"]))
    if definition is not None and definition.specimen and observation.specimen and definition.specimen != observation.specimen:
        issues.append(issue('specimen_conflict', ['specimen', 'raw_name']))
    if definition is not None and definition.specimen and not observation.specimen:
        issues.append(issue('specimen_unknown', ['specimen']))
    expected_type = _result_type(observation.raw_value)
    if expected_type is None or expected_type != observation.result_type:
        issues.append(issue("type_conflict", ["raw_value"]))
    if observation.result_type in {ResultType.NUMERIC, ResultType.COMPARATOR}:
        bound = re.sub(r'^[<>≤≥]=?\s*', '', unicodedata.normalize('NFKC', observation.raw_value).strip())
        if numeric_value(bound) is None:
            issues.append(issue('numeric_unsupported', ['raw_value']))
    if definition is not None and definition.result_types and observation.result_type.lower() not in definition.result_types:
        issues.append(issue("type_conflict", ["raw_value"]))
    if definition is not None and observation.result_type in {ResultType.NUMERIC, ResultType.COMPARATOR}:
        if not definition.unit_forms or _unit_key(observation.raw_unit) not in {_unit_key(unit) for unit in definition.unit_forms}:
            issues.append(issue("unit_unknown", ["raw_unit"]))
    reference = parse_reference_range(observation.reference_range_raw)
    if reference["kind"] == "unknown":
        issues.append(issue(reference["reason"], ["reference_range_raw"]))
    issues.extend(_date_issues(observation))
    if getattr(observation, "reported_error", False):
        issues.append(issue("reported_error", ["raw_name", "raw_value", "raw_unit"]))
    if getattr(observation, "revision_conflict", False):
        issues.append(issue("revision_conflict", ["raw_name", "raw_value", "raw_unit"]))
    if not ({item["code"] for item in issues} - resolved) & TREND_BLOCKING_ISSUES:
        issues.extend(_history_issues(observation, previous, rules, dictionary))
        issues.extend(_internal_issues(observation, previous, rules, dictionary))
    unique = {}
    for item in issues:
        code = item.get("code", "recognition_uncertain")
        if code in resolved:
            continue
        normalized = issue(code, item.get("fields", ()), details=item.get("details", ""),
                           rule_version=item.get("rule_version", VALIDATION_RULE_VERSION), rule_id=item.get("rule_id", code))
        unique[(code, normalized["rule_id"])] = normalized
    return tuple(unique.values())


def reference_comparison(observation, *, dictionary=None, rules=None):
    unknown = {"label": "无法对照", "status": "unavailable"}
    if {item["code"] for item in validate_observation(observation, dictionary=dictionary, rules=rules)} & REFERENCE_BLOCKING_ISSUES:
        return unknown
    reference = parse_reference_range(observation.reference_range_raw)
    if reference["kind"] == "enumeration" and observation.result_type in {ResultType.QUALITATIVE, ResultType.SEMI_QUANTITATIVE}:
        matches = observation.raw_value.strip().casefold() in {value.casefold() for value in reference["values"]}
        return {"label": "范围内" if matches else "与参考不一致", "status": "within" if matches else "different"}
    if reference["kind"] != "numeric":
        return unknown
    low, high = (numeric_value(reference[key]) for key in ("low", "high"))
    if observation.result_type == ResultType.NUMERIC:
        value = numeric_value(observation.raw_value)
        if value is None:
            return unknown
        if low is not None and (value < low or value == low and not reference["low_inclusive"]):
            return {"label": "低于", "status": "below"}
        if high is not None and (value > high or value == high and not reference["high_inclusive"]):
            return {"label": "高于", "status": "above"}
        return {"label": "范围内", "status": "within"}
    if observation.result_type == ResultType.COMPARATOR:
        match = re.fullmatch(rf"([<>]=?|≤|≥)\s*({NUMBER})", unicodedata.normalize("NFKC", observation.raw_value))
        if match:
            operator, bound = match.groups()
            value = numeric_value(bound)
            if value is None:
                return unknown
            inclusive = "=" in operator or operator in {"≤", "≥"}
            if operator[0] in {"<", "≤"} and low is not None and (value < low or value == low and not inclusive):
                return {"label": "低于", "status": "below"}
            if operator[0] in {">", "≥"} and high is not None and (value > high or value == high and not inclusive):
                return {"label": "高于", "status": "above"}
    return unknown
