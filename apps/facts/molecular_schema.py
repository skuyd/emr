"""Application metadata around unchanged source-reported A0 molecular values."""
from collections import Counter
from uuid import UUID

from django.core.exceptions import ValidationError

from . import molecular_contracts as values
from .pathology_schema import SOURCE_ROLES as IHC_ROLES

SCHEMA = "MOLECULAR_REPORT_V1"
CONTEXT = "MOLECULAR_CONTEXT_V1"
DATE_KEYS = frozenset({"assay.collection_date", "assay.received_date", "assay.report_date"})
SHARED_KEYS = frozenset({"specimen.identity", "specimen.description", "specimen.site", "specimen.procedure",
                         "assay.identity", "assay.method", "assay.antibody", "ihc.marker", "ihc.result", "ihc.score", *DATE_KEYS})
TARGET_KEYS = {"SPECIMEN": "specimen.identity", "ASSAY": "assay.identity", "VARIANT": "variant.identity",
               "DRUG_EVIDENCE": "drug_evidence.drugs"}
SOURCE_ROLES = frozenset({*IHC_ROLES, "REPORT_DRUG_EVIDENCE"})
ASSERTIONS = frozenset({"AS_REPORTED_NO_POSITIVITY_INFERRED", "POSITIVE", "DETECTED", "NEGATIVE", "NOT_DETECTED",
                        "UNCERTAIN", "NOT_TESTED", "NOT_PROVIDED"})
ASSAY_MEMBERS = frozenset({"specimen.description", "specimen.site", "specimen.procedure", "assay.name", "assay.panel_name",
                          "assay.panel_size", "assay.molecular_method", *DATE_KEYS})
COMPONENT_KEYS = frozenset("variant." + key for key in
                         ("gene", "expression", "coding", "protein", "codon", "transcript", "location", "change", "tier"))
DRUG_MEMBERS = frozenset({"drug_evidence.statement", "drug_evidence.direction", "drug_evidence.context"})


def molecular_fields(field_spec):
    result = {}
    for key, spec in values.FIELDS.items():
        if key in DATE_KEYS:
            continue
        roles = ["SPECIMEN", "ASSAY"]
        rank = 21 if key in {"assay.name", "assay.panel_name", "assay.panel_size", "assay.molecular_method"} else 40
        if key == "variant.identity":
            rank = 30
        elif key.startswith("variant."):
            roles.append("VARIANT")
            rank = 31 if key in COMPONENT_KEYS else 40
        elif key.startswith("drug_evidence."):
            roles.append("VARIANT")
            rank = 50 if key == "drug_evidence.drugs" else 60 if key == "drug_evidence.level" else 51
            if key != "drug_evidence.drugs":
                roles.append("DRUG_EVIDENCE")
        result[key] = field_spec(spec.label, spec.value_type, spec.entity_kind, spec.codes, SCHEMA, "MOLECULAR", rank, tuple(roles))
    result["assay.negative_statement"] = field_spec("原文检测范围与结果", "MOLECULAR_NEGATIVE", "assay", (), SCHEMA,
                                                  "MOLECULAR", 40, ("SPECIMEN", "ASSAY"))
    return result


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValidationError("分子应用字段结构无效。")


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValidationError("分子字段状态或角色无效。")


def _text(value, maximum=30000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("请保留有界且非空的原件文字。")


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("分子关联身份须为明确 UUID。") from None


def _ordinals(value):
    if (not isinstance(value, list) or len(value) > 100 or any(type(i) is not int or i < 0 for i in value)
            or len(set(value)) != len(value)):
        raise ValidationError("来源依据须为本字段不同的实际片段序号。")


def validate_value(key, value):
    if key != "assay.negative_statement":
        return values.validate_value(key, value)
    _shape(value, {"text", "assertion", "scope"})
    _text(value["text"])
    _enum(value["assertion"], {"NEGATIVE", "NOT_DETECTED", "UNCERTAIN", "NOT_TESTED", "NOT_PROVIDED"})
    from .molecular_source_codes import validate_assertion_code, validate_detection_code
    validate_assertion_code(value["assertion"], value["text"])
    scope = value["scope"]
    _shape(scope, {"state", "raw", "detection_kinds", "targets", "limitations"})
    _enum(scope["state"], {"EXPLICIT", "UNKNOWN"})
    for key in ("detection_kinds", "targets", "limitations"):
        if not isinstance(scope[key], list) or len(scope[key]) > 32:
            raise ValidationError("检测范围须是有界的原文列表，不能截断或扩展。")
    if scope["state"] == "UNKNOWN":
        if scope["raw"] is not None or any(scope[key] for key in ("detection_kinds", "targets", "limitations")):
            raise ValidationError("未知检测范围不能带入推测目标。")
    else:
        _text(scope["raw"], 4096)
        if not scope["detection_kinds"]:
            raise ValidationError("明确范围必须保留原检测种类。")
        for item in scope["detection_kinds"]:
            _shape(item, {"code", "raw"})
            _enum(item["code"], {"SMALL_VARIANT", "COPY_NUMBER", "FUSION", "MSI", "TMB", "OTHER"})
            _text(item["raw"], 4096)
            validate_detection_code(item)
        for item in [*scope["targets"], *scope["limitations"]]:
            _text(item, 4096)
    return value


def display_value(key, value):
    validate_value(key, value)
    if key == "assay.negative_statement":
        return value["text"] + ("（检测范围未判断）" if value["scope"]["state"] == "UNKNOWN" else "")
    result = values.display_value(key, value)
    if values.FIELDS[key].value_type == "COMPONENT" and value["state"] != "PRINTED":
        return {"NOT_PRINTED": "原件未印刷", "UNKNOWN": "原文尚未判断"}[value["state"]]
    if key == "variant.identity" and value["status"] == "INCOMPLETE":
        result += "（原身份组件不完整）"
    return result


def validate_assertion(value):
    _shape(value, {"code", "raw", "proof_fragment_ordinals"})
    _enum(value["code"], ASSERTIONS)
    _ordinals(value["proof_fragment_ordinals"])
    if value["code"] == "AS_REPORTED_NO_POSITIVITY_INFERRED":
        if value["raw"] is not None or value["proof_fragment_ordinals"]:
            raise ValidationError("无明确断言时不得补造原文或依据。")
    else:
        _text(value["raw"])
        if not value["proof_fragment_ordinals"]:
            raise ValidationError("明示结果须有本字段的原文依据。")
        from .molecular_source_codes import validate_assertion_code
        validate_assertion_code(value["code"], value["raw"])


def validate_context_shape(key, context):
    from .clinical_schema import FIELDS

    _shape(context, {"context_version", "report_id", "membership_policy", "bindings", "association"})
    if context["context_version"] != CONTEXT or context["membership_policy"] != CONTEXT:
        raise ValidationError("不支持的分子上下文模式。")
    _uuid(context["report_id"])
    bindings = context["bindings"]
    if not isinstance(bindings, list) or len(bindings) > 11:
        raise ValidationError("分子关联角色数量无效。")
    counts, variant_targets = Counter(), []
    drug = key.startswith("drug_evidence.")
    for binding in bindings:
        _shape(binding, {"role", "state", "target_fact_id", "target_entity_key", "proof_fragment_ordinals", "reason"})
        role = binding["role"]
        _enum(role, FIELDS[key].roles)
        counts[role] += 1
        _ordinals(binding["proof_fragment_ordinals"])
        if binding["state"] == "BOUND":
            _uuid(binding["target_fact_id"])
            _text(binding["target_entity_key"], 100)
            if not binding["proof_fragment_ordinals"] or binding["reason"] is not None:
                raise ValidationError("已绑定身份必须有原文依据且没有未知原因。")
        elif binding["state"] == "UNKNOWN":
            if binding["target_fact_id"] is not None or binding["target_entity_key"] is not None:
                raise ValidationError("未关联须保留空目标。")
            _enum(binding["reason"], {"NOT_STATED", "AMBIGUOUS", "SOURCE_INCOMPLETE", "UNKNOWN"})
        else:
            raise ValidationError("未知分子关联状态。")
        if role == "VARIANT":
            variant_targets.append(binding)
    if set(counts) != set(FIELDS[key].roles) or any(count != 1 for role, count in counts.items() if not (drug and role == "VARIANT")):
        raise ValidationError("必需角色必须恰好声明一次。")
    if not drug:
        if context["association"] is not None:
            raise ValidationError("普通分子字段不能带入药物关联声明。")
        return
    if not 1 <= len(variant_targets) <= 8 or len({(r['state'], r['target_fact_id']) for r in variant_targets}) != len(variant_targets):
        raise ValidationError("药物关联须保留一至八个不同的原变异身份。")
    association = context["association"]
    _shape(association, {"state", "raw", "proof_fragment_ordinals"})
    _ordinals(association["proof_fragment_ordinals"])
    if any(row["state"] == "UNKNOWN" for row in variant_targets):
        if (len(variant_targets) != 1 or association["state"] != "UNKNOWN" or association["raw"] is not None
                or association["proof_fragment_ordinals"]):
            raise ValidationError("不明药物关联不能混入确定的变异集合。")
    else:
        if association["state"] != "EXPLICIT" or not association["proof_fragment_ordinals"]:
            raise ValidationError("药物须有明确关联原文，不能只给一组身份。")
        _text(association["raw"])


def field_allowed_in_report(key, routing_kind):
    from .clinical_schema import FIELDS

    spec = FIELDS.get(key)
    if spec is None:
        return False
    if routing_kind == "MOLECULAR":
        return spec.version == SCHEMA or key in SHARED_KEYS
    return spec.version != SCHEMA and (spec.category != "PATHOLOGY" or routing_kind == "PATHOLOGY")
