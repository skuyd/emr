"""Versioned report fields. Values describe the source; they do not diagnose."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from django.core.exceptions import ValidationError


SCHEMA_VERSION = "1.1"


@dataclass(frozen=True)
class FieldSpec:
    label: str
    value_type: str
    entity_kind: str
    codes: tuple = ()
    version: str = "1.0"
    category: str = "IMAGING"
    rank: int = 0
    roles: tuple = ()


FIELDS = {
    "report.exam_date": FieldSpec("检查日期", "DATE", "report"),
    "imaging.modality": FieldSpec("检查方法", "CODED", "report", ("CT", "MR", "US", "PET_CT", "XRAY")),
    "imaging.body_site": FieldSpec("检查部位", "TEXT", "report"),
    "lesion.site": FieldSpec("病灶位置", "TEXT", "lesion"),
    "lesion.laterality": FieldSpec("原文侧别", "CODED", "lesion", ("LEFT", "RIGHT", "BILATERAL", "MIDLINE")),
    "lesion.scoped_laterality": FieldSpec("部位组内的原文侧别", "SCOPED_LATERALITY", "lesion"),
    "lesion.dimensions": FieldSpec("病灶尺寸", "DIMENSIONS", "lesion"),
    "imaging.impression": FieldSpec("报告结论", "TEXT", "report"),
    "lesion.suvmax": FieldSpec("原文 SUVmax", "SCALAR", "lesion", version="1.1"),
    "lesion.maximum_scope": FieldSpec("原文最大/较大限定", "CODED", "lesion", ("GROUP_LARGER", "REPORT_MAXIMUM"), version="1.1"),
    "comparison.statement": FieldSpec("对比原文", "TEXT", "comparison", version="1.1"),
    "comparison.reference_date": FieldSpec("对比引用日期", "DATE", "comparison", version="1.1"),
}
from .pathology_schema import pathology_fields

FIELDS.update(pathology_fields(FieldSpec))
AXES = {None, "LONG", "SHORT", "DIAMETER", "WIDTH", "HEIGHT", "DEPTH", "AP", "TRANSVERSE", "CRANIOCAUDAL"}


def _text(value, *, allow_empty=False, maximum=30000):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ValidationError("字段文字不能为空或超出允许长度。")


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValidationError("结构化字段的值形状无效。")


def validate_value(key, value):
    spec = FIELDS.get(key)
    if spec is None:
        raise ValidationError("未知结构化字段。")
    if spec.value_type == "SCOPED_LATERALITY":
        from .laterality_schema import validate_scoped_value
        validate_scoped_value(value)
    elif spec.value_type == "TEXT":
        _shape(value, {"text"})
        _text(value["text"])
    elif spec.value_type == "CODED":
        _shape(value, {"code", "raw"})
        if value["code"] not in spec.codes:
            raise ValidationError("请选择字段支持的原文类别。")
        _text(value["raw"], maximum=512)
    elif spec.value_type == "DATE":
        _shape(value, {"value", "precision"})
        precision, actual = value["precision"], value["value"]
        if precision == "UNKNOWN":
            if actual is not None:
                raise ValidationError("未知日期不能包含推测值。")
        else:
            pattern = {"YEAR": r"\d{4}", "MONTH": r"\d{4}-\d{2}", "DAY": r"\d{4}-\d{2}-\d{2}"}.get(precision)
            if pattern is None or not isinstance(actual, str) or not re.fullmatch(pattern, actual):
                raise ValidationError("日期必须保留实际精度。")
            try:
                date.fromisoformat(actual + {"YEAR": "-01-01", "MONTH": "-01", "DAY": ""}[precision])
            except ValueError:
                raise ValidationError("日期值无效。") from None
    elif spec.value_type == "DIMENSIONS":
        _shape(value, {"components", "approximate", "measurement_role", "raw"})
        if (not isinstance(value["components"], list) or not 1 <= len(value["components"]) <= 3
                or type(value["approximate"]) is not bool or value["measurement_role"] not in {"CURRENT", "HISTORICAL", "UNKNOWN"}):
            raise ValidationError("尺寸须保留一至三维、原文限定及时间角色。")
        _text(value["raw"], maximum=512)
        for component in value["components"]:
            _shape(component, {"value", "unit", "axis"})
            raw = component["value"]
            if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                raise ValidationError("尺寸数值须为有限十进制文字。")
            try:
                number = Decimal(raw)
            except InvalidOperation:
                raise ValidationError("尺寸数值无效。") from None
            if not number.is_finite() or number < 0 or component["unit"] not in {"mm", "cm", "毫米", "厘米"}:
                raise ValidationError("尺寸须保留非负数值和明确原单位。")
            if component["axis"] not in AXES:
                raise ValidationError("测量轴必须来自明确原文。")
    elif spec.value_type == "SCALAR":
        _shape(value, {"values", "comparator", "unit", "approximate", "measurement_role", "raw"})
        numbers = value["values"]
        if (not isinstance(numbers, list) or value["comparator"] not in {"EQ", "LT", "LE", "GT", "GE", "RANGE"}
                or len(numbers) != (2 if value["comparator"] == "RANGE" else 1)
                or type(value["approximate"]) is not bool
                or value["measurement_role"] not in {"CURRENT", "HISTORICAL", "UNKNOWN"}):
            raise ValidationError("数值须保留原文的范围、比较符号及时间角色。")
        for raw in numbers:
            if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+)?", raw) or len(raw) > 30:
                raise ValidationError("数值须为非负有限十进制文字。")
        if len(numbers) == 2 and Decimal(numbers[0]) > Decimal(numbers[1]):
            raise ValidationError("范围下限不能大于上限；请核对原件，不能自动交换。")
        if value["unit"] is not None:
            _text(value["unit"], maximum=30)
        _text(value["raw"], maximum=512)
    else:
        from .pathology_schema import validate_pathology_value

        validate_pathology_value(spec.value_type, value)
    return value


def display_value(key, value):
    spec = FIELDS[key]
    if spec.value_type == "SCOPED_LATERALITY":
        from .laterality_schema import display_scoped_value
        return display_scoped_value(value)
    if spec.value_type == "TEXT":
        return value["text"]
    if spec.value_type == "DATE":
        return value["value"] or "时间不详"
    if spec.value_type == "CODED":
        return value["raw"]
    if spec.value_type == "SCALAR":
        qualifier = {"EQ": "", "LT": "<", "LE": "≤", "GT": ">", "GE": "≥", "RANGE": ""}[value["comparator"]]
        rendered = ("约" if value["approximate"] else "") + qualifier + "～".join(value["values"])
        if value["unit"]:
            rendered += " " + value["unit"]
        return rendered + {"CURRENT": "", "HISTORICAL": "（历史记录值）", "UNKNOWN": "（时间角色不详）"}[value["measurement_role"]]
    if spec.value_type == "DIMENSIONS":
        return value["raw"] + {"CURRENT": "", "HISTORICAL": "（历史记录值）", "UNKNOWN": "（时间角色不详）"}[value["measurement_role"]]
    from .pathology_schema import display_pathology_value

    return display_pathology_value(spec.value_type, value)


def field_content(key, value, raw_value, *, limitations=(), transformations=(), entity_context=None, source_role=None, semantic_qualifiers=None):
    validate_value(key, value)
    _text(raw_value)
    spec = FIELDS[key]
    content = {
        # Common excerpt keys remain available to existing display adapters.
        "category": spec.category, "text": f"{spec.label}：{display_value(key, value)}",
        "date": None, "date_raw": "", "date_precision": "UNKNOWN", "institution": "",
        "record_date": None, "dates": [], "limitations": list(limitations),
        # A newly introduced field does not rewrite the schema identity of old
        # immutable candidates or make their existing confirmations obsolete.
        "schema_version": spec.version, "field_key": key, "value_type": spec.value_type,
        "result_type": "SOURCE_REPORTED", "value": deepcopy(value), "raw_value": raw_value,
        "transformations": list(transformations),
    }
    if spec.category == "PATHOLOGY":
        content.update(entity_context=deepcopy(entity_context), source_role=source_role,
                       semantic_qualifiers=deepcopy(semantic_qualifiers or {}))
        validate_content(content)
    elif entity_context is not None or source_role is not None or semantic_qualifiers is not None:
        raise ValidationError("旧字段模式不能混入新关联属性。")
    return content


def validate_content(content, *, field_key=None):
    if not isinstance(content, dict):
        raise ValidationError("结构化候选内容无效。")
    key = content.get("field_key")
    if field_key is not None and key != field_key:
        raise ValidationError("字段身份不可更改。")
    validate_value(key, content.get("value"))
    if (content.get("schema_version") != FIELDS[key].version or content.get("value_type") != FIELDS[key].value_type
            or content.get("result_type") != "SOURCE_REPORTED" or content.get("category") != FIELDS[key].category):
        raise ValidationError("字段模式或报告类型不匹配。")
    _text(content.get("raw_value"))
    if content.get("text") != f"{FIELDS[key].label}：{display_value(key, content['value'])}":
        raise ValidationError("字段显示文字与有效值不一致。")
    if FIELDS[key].category == "PATHOLOGY":
        from .pathology_schema import SOURCE_ROLES, validate_context_shape

        validate_context_shape(key, content.get("entity_context"))
        if content.get("source_role") not in SOURCE_ROLES or not isinstance(content.get("semantic_qualifiers"), dict):
            raise ValidationError("新字段须保留原文角色与独立语义限定。")
        if "literal_source" in content:
            from .pathology_source import validate_shape

            validate_shape(content["literal_source"])
    elif "literal_source" in content:
        raise ValidationError("旧字段模式不能混入病理来源角色。")
    return content
