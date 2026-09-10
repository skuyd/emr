"""Source-reported pathology/IHC values and immutable, typed report bindings."""
import re
import uuid

from django.core.exceptions import ValidationError


SCHEMA = "PATHOLOGY_IHC_V1"
CONTEXT = "IHC_CONTEXT_V1"
SOURCE_ROLES = {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA", "SUBMITTED_HISTORY", "HISTORICAL_QUOTE",
                "EXPLANATION", "CONTROL", "QC", "UNKNOWN"}
ASSERTIONS = {"SOURCE_TEXT_ONLY_NOT_DIAGNOSED", "AS_REPORTED_NO_POSITIVITY_INFERRED", "POSITIVE", "NEGATIVE",
              "DETECTED", "NOT_DETECTED", "UNCERTAIN", "NOT_TESTED", "NOT_PROVIDED"}
TARGET_KEYS = {"SPECIMEN": "specimen.identity", "ASSAY": "assay.identity", "MARKER": "ihc.marker"}
MEMBER_KEYS = {"specimen.description", "specimen.site", "specimen.procedure", "assay.method", "assay.antibody"}


def pathology_fields(field_spec):
    def item(label, value_type, entity, rank, roles=(), codes=()):
        return field_spec(label, value_type, entity, codes, SCHEMA, "PATHOLOGY", rank, roles)

    fields = {
        "specimen.identity": item("标本原文身份", "IDENTITY", "specimen", 10),
        "pathology.reported_stage": item("报告记载分期", "ASSERTED_TEXT", "report", 12),
        "assay.identity": item("检测原文身份", "IDENTITY", "assay", 20, ("SPECIMEN",)),
        "assay.method": item("原文检测方法", "CODED", "assay", 21, ("SPECIMEN", "ASSAY"), ("IHC", "ISH", "OTHER", "UNKNOWN")),
        "assay.antibody": item("原文抗体", "TEXT", "assay", 21, ("SPECIMEN", "ASSAY")),
        "ihc.marker": item("原文IHC标记", "MARKER", "ihc", 30, ("SPECIMEN", "ASSAY")),
        "ihc.result": item("原文IHC结果", "ASSERTED_TEXT", "ihc", 40, ("SPECIMEN", "ASSAY", "MARKER")),
        "ihc.score": item("原文IHC评分", "IHC_SCORE", "ihc", 40, ("SPECIMEN", "ASSAY", "MARKER")),
    }
    for key, label in [("description", "标本原文描述"), ("site", "标本原文部位"), ("procedure", "原文取材方式")]:
        fields["specimen." + key] = item(label, "TEXT", "specimen", 11, ("SPECIMEN",))
    for key, label in [("histology", "组织学原文"), ("differentiation", "分化原文"), ("invasion", "浸润原文"),
                       ("margin", "切缘原文"), ("reported_stage", "标本记载分期")]:
        fields["specimen." + key] = item(label, "ASSERTED_TEXT", "specimen", 12, ("SPECIMEN",))
    fields["specimen.dimensions"] = item("标本或肿瘤原文尺寸", "PATHOLOGY_DIMENSIONS", "specimen", 12, ("SPECIMEN",))
    fields["specimen.nodes"] = item("病理淋巴结原文计数", "NODE_COUNTS", "specimen", 12, ("SPECIMEN",))
    for key, label in [("collection_date", "原文采样日期"), ("received_date", "原文收样日期"), ("report_date", "原文报告日期")]:
        fields["assay." + key] = item(label, "DATE", "assay", 21, ("SPECIMEN", "ASSAY"))
    return fields


def _uuid(value):
    if not isinstance(value, str):
        raise ValidationError("上下文身份须为明确的 UUID。")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValidationError("上下文身份须为明确的 UUID。") from None


def validate_context_shape(key, context):
    from .clinical_schema import FIELDS, _shape, _text

    _shape(context, {"context_version", "report_id", "membership_policy", "bindings"})
    if context["context_version"] != CONTEXT or context["membership_policy"] != CONTEXT:
        raise ValidationError("不支持的字段上下文模式。")
    _uuid(context["report_id"])
    bindings = context["bindings"]
    if not isinstance(bindings, list) or len(bindings) != len(FIELDS[key].roles):
        raise ValidationError("每个必需上下文角色必须明确绑定或标为未关联。")
    roles = []
    for binding in bindings:
        _shape(binding, {"role", "state", "target_fact_id", "target_entity_key", "proof_fragment_ordinals", "reason"})
        role = binding["role"]
        if role not in TARGET_KEYS or role in roles:
            raise ValidationError("上下文角色类型无效或重复。")
        roles.append(role)
        ordinals = binding["proof_fragment_ordinals"]
        if (not isinstance(ordinals, list) or len(ordinals) > 100 or any(type(i) is not int or i < 0 for i in ordinals)
                or len(ordinals) != len(set(ordinals))):
            raise ValidationError("关联依据必须是本字段的不同原文片段序号。")
        if binding["state"] == "BOUND":
            _uuid(binding["target_fact_id"])
            _text(binding["target_entity_key"], maximum=100)
            if not ordinals or binding["reason"] is not None:
                raise ValidationError("已绑定上下文须有自己的原文依据，不能使用空目标。")
        elif binding["state"] == "UNKNOWN":
            if (binding["target_fact_id"] is not None or binding["target_entity_key"] is not None
                    or binding["reason"] not in {"NOT_STATED", "AMBIGUOUS", "SOURCE_INCOMPLETE", "UNKNOWN"}):
                raise ValidationError("未关联须明确空目标及原因，不能保留暗示性目标。")
        else:
            raise ValidationError("未知的字段关联状态。")
    if set(roles) != set(FIELDS[key].roles):
        raise ValidationError("字段的上下文角色不完整。")


def validate_pathology_value(kind, value):
    from .clinical_schema import _shape, _text, validate_value

    if kind == "IDENTITY":
        _shape(value, {"label", "raw"})
        _text(value["label"], maximum=256)
        _text(value["raw"], maximum=1000)
    elif kind == "MARKER":
        _shape(value, {"code", "label", "raw"})
        if value["code"] is not None and (not isinstance(value["code"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,39}", value["code"])):
            raise ValidationError("标记编码须明确；没有编码时保留空值。")
        _text(value["label"], maximum=80)
        if any(c in value["label"] for c in "\n\r：:；;。"):
            raise ValidationError("标记名称不能带入整句结果或检测上下文。")
        _text(value["raw"], maximum=1000)
    elif kind == "ASSERTED_TEXT":
        _shape(value, {"text", "assertion"})
        _text(value["text"])
        if value["assertion"] not in ASSERTIONS:
            raise ValidationError("请保留报告的明确否定或不确定，不推导医学结论。")
    elif kind == "IHC_SCORE":
        _shape(value, {"score_kind", "values", "comparator", "unit", "unit_state", "scale_kind", "approximate", "assertion", "raw"})
        if value["score_kind"] not in {"TPS", "CPS", "IC"}:
            raise ValidationError("评分类型须由原文明示。")
        if value["scale_kind"] != ("SCORE" if value["score_kind"] == "CPS" else "PROPORTION"):
            raise ValidationError("CPS 分数与 TPS/IC 比例不能互换。")
        if value["unit_state"] != ("NOT_PRINTED" if value["unit"] is None else "PRINTED"):
            raise ValidationError("评分单位须保留原文缺失状态，不能默认百分号。")
        if value["assertion"] not in {"AS_REPORTED_NO_POSITIVITY_INFERRED", "POSITIVE", "NEGATIVE", "UNCERTAIN"}:
            raise ValidationError("数值不能推导阳性，未检测也不能写成零值。")
        scalar = {key: value[key] for key in {"values", "comparator", "unit", "approximate", "raw"}}
        validate_value("lesion.suvmax", {**scalar, "measurement_role": "CURRENT"})
    elif kind == "PATHOLOGY_DIMENSIONS":
        from .clinical_schema import AXES

        _shape(value, {"components", "approximate", "measurement_role", "measurement_object", "raw"})
        if value["measurement_object"] not in {"SPECIMEN", "TUMOR", "UNKNOWN"}:
            raise ValidationError("原文尺寸须区分整体标本与肿瘤，不能自行认定。")
        if (not isinstance(value["components"], list) or not 1 <= len(value["components"]) <= 3
                or type(value["approximate"]) is not bool or value["measurement_role"] not in {"CURRENT", "HISTORICAL", "UNKNOWN"}):
            raise ValidationError("尺寸须保留一至三维、原文限定及时间角色。")
        _text(value["raw"], maximum=512)
        for component in value["components"]:
            _shape(component, {"value", "unit", "axis"})
            raw = component["value"]
            if not isinstance(raw, str) or len(raw) > 30 or not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                raise ValidationError("尺寸须为非负有限十进制文字。")
            if component["unit"] not in {None, "mm", "cm", "毫米", "厘米"} or component["axis"] not in AXES:
                raise ValidationError("尺寸原单位和测量轴必须来自原文；未注明时保留空值。")
    elif kind == "NODE_COUNTS":
        _shape(value, {"groups", "assertion", "raw"})
        if not isinstance(value["groups"], list) or not 1 <= len(value["groups"]) <= 100 or value["assertion"] not in ASSERTIONS:
            raise ValidationError("请逐组保留原文计数与限定。")
        for group in value["groups"]:
            _shape(group, {"label", "sampled", "positive", "raw"})
            _text(group["label"], maximum=256)
            _text(group["raw"], maximum=1000)
            for key in ("sampled", "positive"):
                if group[key] is not None and (not isinstance(group[key], str) or not re.fullmatch(r"\d{1,8}", group[key])):
                    raise ValidationError("淋巴结计数保留原非负整数文字，未提供时为空。")
        _text(value["raw"])
    else:
        raise ValidationError("未知病理值类型。")


def display_pathology_value(kind, value):
    if kind in {"IDENTITY", "MARKER"}:
        return value["label"]
    if kind == "ASSERTED_TEXT":
        return value["text"]
    if kind == "IHC_SCORE":
        qualifier = {"EQ": "", "LT": "<", "LE": "≤", "GT": ">", "GE": "≥", "RANGE": ""}[value["comparator"]]
        number = ("约" if value["approximate"] else "") + qualifier + "～".join(value["values"])
        return value["score_kind"] + " " + number + (" " + value["unit"] if value["unit"] is not None else "（单位未印刷）")
    return value["raw"]


def slot(value):
    """Every anchor identity component; spelling changes need replacement too."""
    if "score_kind" in value:
        return value["score_kind"]
    if "code" in value and "label" in value:
        return value["code"], value["label"], value["raw"]
    if "label" in value:
        return value["label"], value["raw"]
    return None
