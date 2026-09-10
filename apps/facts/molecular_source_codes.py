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
    if re.search(r"不确定|可疑|可能|不(?:能)?排除|\buncertain\b|\bindeterminate\b|\bequivocal\b", text, re.I):
        return {"UNCERTAIN"}
    patterns = {
        "NOT_DETECTED": r"未检出|没有检出|未检测出|未检测到|\bnot\s+detected\b|\bno\b.{0,120}\b(?:change|variant|variation|mutation|fusion|alteration)s?\b",
        "NOT_TESTED": r"未检测(?!到)|未做检测|\bnot\s+tested\b",
        "NOT_PROVIDED": r"未提供|未出具|\bnot\s+(?:provided|reported)\b",
        "NEGATIVE": r"阴性|未见|\bnegative\b",
    }
    found = set()
    for code, pattern in patterns.items():
        if re.search(pattern, text, re.I):
            found.add(code)
            text = re.sub(pattern, " ", text, flags=re.I)
    # A negated positive label is not positive; do not infer a negative clinical
    # result from it either. Unknown wording remains unclassified.
    text = re.sub(r"(?:未|不|非|没有)[^，。；;,.]{0,8}阳性|\b(?:not|no)\s+positive\b", " ", text, flags=re.I)
    if re.search(r"阳性|\bpositive\b", text, re.I):
        found.add("POSITIVE")
    if re.search(r"(?<!未)(?<!不)检出|\bdetected\b", text, re.I):
        found.add("DETECTED")
    negative, positive = {"NOT_DETECTED", "NEGATIVE"}, {"POSITIVE", "DETECTED"}
    if (found & negative and found & positive) or (found & {"NOT_TESTED", "NOT_PROVIDED"} and len(found) > 1):
        return set()
    return found


def validate_assertion_code(code, raw):
    found = assertion_codes(raw)
    if code not in found:
        raise ValidationError("原文不能支持所选结果代码；请保留未判断状态，不得反转原断言。")
