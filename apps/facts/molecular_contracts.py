"""Pure source-reported molecular values, independent of storage and extraction.

These validators check value structure, never source truth, clinical relevance,
specimen/assay binding or permission to use a result. COMPLETE describes only the
declared identity components; NOT_PRINTED requires proof outside this module.
There is deliberately no registration into clinical_schema or the IHC schema.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from types import MappingProxyType

from django.core.exceptions import ValidationError


SCHEMA_VERSION = "MOLECULAR_VALUE_CONTRACT_V1"


@dataclass(frozen=True)
class FieldSpec:
    label: str
    value_type: str
    entity_kind: str
    codes: tuple[str, ...] = ()


FIELDS = MappingProxyType({
    "assay.name": FieldSpec("检测原名称", "TEXT", "assay"),
    "assay.panel_name": FieldSpec("检测 panel 原名称", "TEXT", "assay"),
    "assay.molecular_method": FieldSpec("分子检测原方法", "TEXT", "assay"),
    "assay.panel_size": FieldSpec("panel 原规模", "PANEL_SIZE", "assay"),
    "assay.collection_date": FieldSpec("原文采样日期", "DATE", "assay"),
    "assay.received_date": FieldSpec("原文收样日期", "DATE", "assay"),
    "assay.report_date": FieldSpec("原文报告日期", "DATE", "assay"),
    "variant.identity": FieldSpec("变异原文身份", "VARIANT_IDENTITY", "variant"),
    "variant.gene": FieldSpec("原文基因或位点", "COMPONENT", "variant"),
    "variant.expression": FieldSpec("完整变异原表达", "COMPONENT", "variant"),
    "variant.coding": FieldSpec("原文编码位点", "COMPONENT", "variant"),
    "variant.protein": FieldSpec("原文蛋白位点", "COMPONENT", "variant"),
    "variant.codon": FieldSpec("原文密码子表达", "COMPONENT", "variant"),
    "variant.transcript": FieldSpec("原文转录本及版本", "COMPONENT", "variant"),
    "variant.location": FieldSpec("原文位置及参考版本", "COMPONENT", "variant"),
    "variant.change": FieldSpec("原文拷贝数变化", "COMPONENT", "variant"),
    "variant.tier": FieldSpec("原文变异分级", "COMPONENT", "variant"),
    "variant.allele_fraction": FieldSpec("原文变异丰度", "ALLELE_FRACTION", "variant"),
    "variant.copy_number": FieldSpec("原文拷贝数", "COPY_NUMBER", "variant"),
    "assay.msi_category": FieldSpec("原文 MSI 类别", "CODED", "assay", ("MSI_H", "MSI_L", "MSS", "UNKNOWN")),
    "assay.msi_value": FieldSpec("原文 MSI 数值", "MSI", "assay"),
    "assay.tmb_value": FieldSpec("原文 TMB 数值", "TMB", "assay"),
    "assay.tmb_qualitative": FieldSpec("原文 TMB 定性", "CODED", "assay", ("HIGH", "INTERMEDIATE", "LOW", "UNKNOWN")),
    "drug_evidence.drugs": FieldSpec("报告药物或组合原文", "DRUG_GROUP", "drug_evidence"),
    "drug_evidence.statement": FieldSpec("报告药物依据原文", "TEXT", "drug_evidence"),
    "drug_evidence.direction": FieldSpec("报告药物依据方向", "CODED", "drug_evidence", (
        "REPORT_BENEFIT", "REPORT_RESISTANCE", "REPORT_NO_BENEFIT", "UNCERTAIN", "NOT_STATED")),
    "drug_evidence.level": FieldSpec("报告证据等级及体系原文", "DRUG_LEVEL", "drug_evidence"),
    "drug_evidence.context": FieldSpec("报告药物依据上下文原文", "TEXT", "drug_evidence"),
})

_COMPONENT_STATES = ("PRINTED", "NOT_PRINTED", "UNKNOWN")
_QUANTITY_KINDS = ("ALLELE_FRACTION", "COPY_NUMBER", "MSI", "TMB", "PANEL_SIZE")
_QUANTITY_KEYS = {"status", "values", "comparator", "unit", "unit_state", "approximate",
                  "measurement_kind", "assertion", "raw"}


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError("分子字段值的形状无效：请保留规定键，不能省略或增加键。")


def _enum(value, options):
    # Check type first: arbitrary JSON lists/dicts must not leak TypeError.
    if not isinstance(value, str) or value not in options:
        raise ValidationError("分子字段的状态或类别无效。")


def _text(value, maximum=30000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("分子字段须保留非空且长度有效的原文。")


def validate_component(value, *, multiple=False):
    """Validate declared printed component(s), without proving their source.

    A scalar has state/raw; a repeated component has state/values. UNKNOWN and
    NOT_PRINTED are distinct declarations, both without invented values. Repeated
    strings preserve order and duplicates; they are not a normalized set.
    """
    _shape(value, {"state", "values"} if multiple else {"state", "raw"})
    _enum(value["state"], _COMPONENT_STATES)
    if multiple:
        values = value["values"]
        if not isinstance(values, list) or len(values) > 32:
            raise ValidationError("重复原文组件须为有界列表；不能静默截断。")
        if value["state"] == "PRINTED":
            if not values:
                raise ValidationError("已印组件须有原文。")
            for raw in values:
                _text(raw, 2048)
        elif values:
            raise ValidationError("未印或未知组件不能含有推测值。")
    elif value["state"] == "PRINTED":
        _text(value["raw"], 4096)
    elif value["raw"] is not None:
        raise ValidationError("未印或未知组件必须保留空原值。")
    return value


def _date(value):
    _shape(value, {"value", "precision", "raw"})
    _text(value["raw"], 2048)
    precision = value["precision"]
    _enum(precision, ("YEAR", "MONTH", "DAY", "UNKNOWN"))
    if precision == "UNKNOWN":
        if value["value"] is not None:
            raise ValidationError("未知日期不能包含补全或猜测日期。")
        return
    patterns = {"YEAR": r"[0-9]{4}", "MONTH": r"[0-9]{4}-[0-9]{2}", "DAY": r"[0-9]{4}-[0-9]{2}-[0-9]{2}"}
    actual = value["value"]
    if not isinstance(actual, str) or re.fullmatch(patterns[precision], actual) is None:
        raise ValidationError("日期值须符合其原有精度。")
    try:
        date.fromisoformat(actual + {"YEAR": "-01-01", "MONTH": "-01", "DAY": ""}[precision])
    except ValueError:
        raise ValidationError("日期值不存在。") from None


def _quantity(value, kind):
    _shape(value, _QUANTITY_KEYS | ({"count_object"} if kind == "PANEL_SIZE" else set()))
    _enum(value["status"], ("PARSED", "UNRESOLVED"))
    _enum(value["measurement_kind"], (kind,))
    # A quantity never represents an inferred positive/negative clinical result.
    _enum(value["assertion"], ("AS_REPORTED_NO_POSITIVITY_INFERRED", "UNCERTAIN"))
    _enum(value["unit_state"], _COMPONENT_STATES)
    _text(value["raw"])
    if type(value["approximate"]) is not bool:
        raise ValidationError("约数限定须为布尔值。")
    if value["unit_state"] == "PRINTED":
        _text(value["unit"], 128)
    elif value["unit"] is not None:
        raise ValidationError("未印或未知单位不能包含补充单位。")
    if kind == "PANEL_SIZE":
        validate_component(value["count_object"])
    numbers = value["values"]
    if not isinstance(numbers, list):
        raise ValidationError("数值须为有序文字列表。")
    if value["status"] == "UNRESOLVED":
        if numbers or value["comparator"] is not None:
            raise ValidationError("未解析原断言只能保留空数值及空比较符，不能补零。")
        return
    comparator = value["comparator"]
    _enum(comparator, ("EQ", "LT", "LE", "GT", "GE", "RANGE"))
    if len(numbers) != (2 if comparator == "RANGE" else 1):
        raise ValidationError("比较符与数值数量不一致。")
    pattern = r"[0-9]+" if kind == "PANEL_SIZE" else r"[0-9]+(?:\.[0-9]+)?"
    for number in numbers:
        if not isinstance(number, str) or len(number) > 64 or re.fullmatch(pattern, number) is None:
            raise ValidationError("数值须为非负有限十进制文字，panel 规模须为整数。")
    if len(numbers) == 2 and Decimal(numbers[0]) > Decimal(numbers[1]):
        raise ValidationError("范围下限不能大于上限；不能自动交换。")


def _variant_identity(value):
    if not isinstance(value, dict):
        raise ValidationError("变异身份须为对象。")
    kind = value.get("kind")
    _enum(kind, ("SMALL_VARIANT", "COPY_NUMBER", "FUSION"))
    extra = {
        "SMALL_VARIANT": {"gene", "expression", "coding", "protein", "codons", "transcripts", "locations"},
        "COPY_NUMBER": {"gene", "change"},
        "FUSION": {"expression", "partners", "order_meaning"},
    }[kind]
    _shape(value, {"kind", "status", "scope", "raw"} | extra)
    _enum(value["status"], ("COMPLETE", "INCOMPLETE"))
    _enum(value["scope"], ("SOMATIC", "GERMLINE", "UNKNOWN"))
    _text(value["raw"])
    declared = []
    required = []

    def check(item, *, multiple=False, required_printed=False):
        validate_component(item, multiple=multiple)
        declared.append(item)
        if required_printed:
            required.append(item)

    if kind == "SMALL_VARIANT":
        check(value["gene"], required_printed=True)
        check(value["expression"], required_printed=True)
        for key in ("coding", "protein", "codons", "transcripts", "locations"):
            check(value[key], multiple=True)
    elif kind == "COPY_NUMBER":
        check(value["gene"], required_printed=True)
        check(value["change"], required_printed=True)
    else:
        check(value["expression"], required_printed=True)
        _enum(value["order_meaning"], ("FIVE_TO_THREE", "AS_PRINTED_UNKNOWN"))
        if not isinstance(value["partners"], list) or len(value["partners"]) != 2:
            raise ValidationError("融合保留两侧伙伴原顺序，不能猜测、合并或截断。")
        for partner in value["partners"]:
            _shape(partner, {"gene", "transcripts", "breakpoints"})
            check(partner["gene"], required_printed=True)
            check(partner["transcripts"], multiple=True)
            check(partner["breakpoints"], multiple=True)
    if value["status"] == "COMPLETE":
        if any(item["state"] != "PRINTED" for item in required) or any(item["state"] == "UNKNOWN" for item in declared):
            raise ValidationError("身份组件未完整时不能声明结构完整。")
        if kind == "FUSION" and value["order_meaning"] == "AS_PRINTED_UNKNOWN":
            raise ValidationError("融合方向未知时须保持结构不完整状态。")


def _drug_group(value):
    _shape(value, {"names", "relation", "raw"})
    _enum(value["relation"], ("SINGLE", "AND", "OR", "ALTERNATIVE", "UNKNOWN"))
    _text(value["raw"])
    names = value["names"]
    if not isinstance(names, list) or not 1 <= len(names) <= 32:
        raise ValidationError("药物或组合须有一至三十二个原名称，不能截断。")
    for name in names:
        _text(name, 2048)
    if value["relation"] == "SINGLE" and len(names) != 1:
        raise ValidationError("单药声明不能含多个药物。")
    if value["relation"] in ("AND", "OR", "ALTERNATIVE") and len(names) < 2:
        raise ValidationError("组合或备选关系不能由单个药物补全。")


def validate_value(key, value):
    """Return the unchanged input after strict value validation.

    Date values include raw for this pure contract; a future storage adapter must
    map it to the shared date field's existing raw-value envelope. No date field
    or second IHC/patient-result schema is registered by calling this function.
    """
    if not isinstance(key, str) or key not in FIELDS:
        raise ValidationError("未知分子值字段。")
    spec = FIELDS[key]
    kind = spec.value_type
    if kind == "TEXT":
        _shape(value, {"text"})
        _text(value["text"])
    elif kind == "COMPONENT":
        validate_component(value)
    elif kind == "DATE":
        _date(value)
    elif kind == "VARIANT_IDENTITY":
        _variant_identity(value)
    elif kind in _QUANTITY_KINDS:
        _quantity(value, kind)
    elif kind == "CODED":
        _shape(value, {"code", "raw"})
        _enum(value["code"], spec.codes)
        _text(value["raw"])
    elif kind == "DRUG_GROUP":
        _drug_group(value)
    elif kind == "DRUG_LEVEL":
        _shape(value, {"grade", "system", "raw"})
        validate_component(value["grade"])
        validate_component(value["system"])
        _text(value["raw"])
    return value


def display_value(key, value):
    """Return plain original text, never normalized HGVS, units or inference.

    An unprinted/unknown scalar component renders as an empty string; callers
    must display its separately retained state. This is not HTML-safe markup.
    """
    validate_value(key, value)
    if FIELDS[key].value_type == "TEXT":
        return value["text"]
    return value["raw"] if value["raw"] is not None else ""
