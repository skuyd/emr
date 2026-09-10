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
    # Consume finite complete phrases before their component words. Residual
    # negation or multiple assertion kinds leave the whole window unclassified:
    # this mapper does not guess which clause/target a qualifier belongs to.
    patterns = {
        "NOT_DETECTED": r"未检出|没有检出|未检测出|未检测到|\bnot\s+detected\b|\bno(?:\s+(?:copy[ -]number|small|sequence|somatic|germline)){0,3}\s+(?:change|variant|variation|mutation|fusion|alteration)s?\b",
        "NOT_TESTED": r"未检测(?![到出])|未做检测|\bnot\s+tested\b",
        "NOT_PROVIDED": r"未提供|未出具|\bnot\s+(?:provided|reported)\b",
        "UNCERTAIN": r"不确定|可疑|可能|不(?:能)?排除|\buncertain\b|\bindeterminate\b|\bequivocal\b",
        "NEGATIVE": r"阴性|未见|\bnegative\b",
        "POSITIVE": r"阳性|\bpositive\b",
        "DETECTED": r"检出|\bdetected\b",
    }
    found = set()
    for code, pattern in patterns.items():
        if re.search(pattern, text, re.I):
            found.add(code)
            text = re.sub(pattern, " ", text, flags=re.I)
    if len(found) != 1 or re.search(r"未|不|无|非|没|\b(?:not|no|never|neither|nor|without|non)\b|n't\b", text, re.I):
        return set()
    return found


def validate_assertion_code(code, raw):
    found = assertion_codes(raw)
    if code not in found:
        raise ValidationError("原文不能支持所选结果代码；请保留未判断状态，不得反转原断言。")
