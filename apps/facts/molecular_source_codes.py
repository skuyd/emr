"""Finite literal labels only; no threshold, clinical inference or external lookup."""
import re
import unicodedata

from django.core.exceptions import ValidationError

DETECTION_LABELS = {
    "SMALL_VARIANT": r"小变异|点突变|单核苷酸变异|插入缺失|\bSNV\b|\bindels?\b|small[ _-]?variants?",
    "COPY_NUMBER": r"拷贝数|\bCNV\b|copy[ _-]?number",
    "FUSION": r"融合|\bfusions?\b",
    "MSI": r"微卫星不稳定|\bMSI\b",
    "TMB": r"肿瘤突变负荷|\bTMB\b",
}


def detection_codes(raw):
    text = unicodedata.normalize("NFKC", raw)
    return {code for code, pattern in DETECTION_LABELS.items() if re.search(pattern, text, re.I)}


def validate_detection_code(item):
    found = detection_codes(item["raw"])
    if item["code"] != "OTHER" and found != {item["code"]}:
        raise ValidationError("检测种类原词不能支持所选代码，请保留其他原词且不要扩大范围。")


def assertion_codes(raw):
    text = " ".join(unicodedata.normalize("NFKC", raw).split())
    # A finite *whole statement* grammar, not keyword detection with a growing
    # list of negators. Unsupported subjects, modifiers or compound statements
    # remain unclassified. In particular, 'absence of detected' cannot match a
    # positive substring, and uncertainty cannot override a second assertion.
    prefix = r"(?:(?:本范围|本次检测|本次|原结果|原件注明|检测范围结论|检测结果|结果断言|结果|结论|result)\s*[:：]?\s*){0,2}"
    cn_object = r"(?:小变异|变异|拷贝数(?:改变|变化)|融合)?"
    en_object = r"(?:copy[ -]number\s+change|small\s+variant|variant|variation|mutation|fusion|alteration)s?"
    patterns = {
        "NOT_DETECTED": rf"(?:未检出|没有检出|未检测出|未检测到){cn_object}|not\s+detected|no\s+{en_object}(?:\s+in\s+the\s+tested\s+scope)?",
        "NOT_TESTED": r"未检测|未做检测|not\s+tested",
        "NOT_PROVIDED": r"未提供|未出具|not\s+(?:provided|reported)",
        "UNCERTAIN": r"不确定|可疑|uncertain|indeterminate|equivocal",
        "NEGATIVE": r"阴性|negative",
        "POSITIVE": r"阳性|positive",
        "DETECTED": rf"(?:明确)?检出{cn_object}|detected",
    }
    return {code for code, pattern in patterns.items() if re.fullmatch(prefix + "(?:" + pattern + ")", text, re.I)}


def validate_assertion_code(code, raw):
    found = assertion_codes(raw)
    if code not in found:
        raise ValidationError("原文不能支持所选结果代码；请保留未判断状态，不得反转原断言。")
