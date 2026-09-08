"""Explicit input validation; dates and ordinals never acquire missing precision."""

from copy import deepcopy
from datetime import date
import re
import uuid

from django.core.exceptions import ValidationError


EVENT_KINDS = {"SYSTEMIC_TREATMENT", "CELL_THERAPY", "RADIOTHERAPY", "SURGERY", "PROCEDURE",
               "LOCAL_PROCEDURE", "ADMISSION", "DISCHARGE", "PAUSE", "DELAY", "STOP", "ASSESSMENT", "MEDICATION_ORDER"}
OCCURRENCES = {"OCCURRED", "PLANNED", "NEGATED", "UNKNOWN", "ORDER"}
EVENT_FIELDS = {"title", "kind", "occurrence", "date", "date_precision", "date_raw", "regimen_text",
                "cycle_ordinal", "cycle_day", "note"}


def operation_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("提交标识无效，请刷新后重试。") from None


def validate_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("请提交当前版本号。")


def validate_day(value, precision):
    patterns = {"DAY": r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "MONTH": r"[0-9]{4}-[0-9]{2}", "YEAR": r"[0-9]{4}"}
    if precision == "UNKNOWN":
        if value is not None and value != "":
            raise ValidationError("未知日期不能同时填写确定日期。")
        return None
    if not isinstance(precision, str) or precision not in patterns or not isinstance(value, str) or not re.fullmatch(patterns[precision], value):
        raise ValidationError("请按所选精度填写日期；不明日期请保留未知。")
    try:
        date.fromisoformat(value + ("-01-01" if precision == "YEAR" else "-01" if precision == "MONTH" else ""))
    except ValueError:
        raise ValidationError("日期不存在，请核对年月日。") from None
    return value


def validate_label(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999):
        raise ValidationError("周期和天数仅支持原文明确或本人更正的正整数；未知请留空。")


def event_content(value):
    payload = deepcopy(value)
    if (not isinstance(payload.get("kind"), str) or payload["kind"] not in EVENT_KINDS
            or not isinstance(payload.get("occurrence"), str) or payload["occurrence"] not in OCCURRENCES):
        raise ValidationError("请选择有效的事件类别和发生状态。")
    for key, maximum in [("title", 200), ("regimen_text", 1000), ("note", 5000), ("date_raw", 200)]:
        text = payload.get(key, "")
        if not isinstance(text, str) or len(text) > maximum or "\x00" in text:
            raise ValidationError("记录内容超出允许范围或含无效字符。")
        payload[key] = text.strip()
    if not payload["title"]:
        raise ValidationError("请填写治疗记录名称。")
    payload["date"] = validate_day(payload.get("date"), payload.get("date_precision"))
    for key in ("cycle_ordinal", "cycle_day"):
        validate_label(payload.get(key))
        payload.setdefault(key, None)
    return payload
