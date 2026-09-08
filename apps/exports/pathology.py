"""Minimum selected pathology meaning; validation dependencies stay private.

Scope aliases are generated for one selection only. They are neither target
Fact IDs nor access grants. This module never queries or infers a diagnosis.
"""
from copy import deepcopy
import uuid

from django.core.exceptions import ValidationError

from apps.facts.clinical_schema import FIELDS, display_value, validate_value
from apps.facts.pathology_schema import CONTEXT, SCHEMA

from .errors import ExportInputError


POLICY = "IHC_SCORE_SEMANTIC_UNIT_V1"
PRIVATE_CONTEXT = "pathology_validation_context"
SOURCE_KEYS = {"document_id", "page", "page_id", "parsing_version", "evidence_id", "polygon", "location", "sha256"}
REPORT_KEYS = {"id", "document_id", "parsing_version", "origin", "routing_kind", "ordinal", "schema_version",
               "status", "revision_number", "revision_id", "pages"}
ASSERTION_LABELS = {
    "SOURCE_TEXT_ONLY_NOT_DIAGNOSED": "保留报告原文，未作诊断推断",
    "AS_REPORTED_NO_POSITIVITY_INFERRED": "按原文记载，未由数值推断阳性",
    "POSITIVE": "原文阳性", "NEGATIVE": "原文阴性", "DETECTED": "原文检出",
    "NOT_DETECTED": "原文未检出", "UNCERTAIN": "原文不确定", "NOT_TESTED": "原文未检测",
    "NOT_PROVIDED": "原文未提供",
}


def is_pathology(row):
    return row.get("schema_version") == SCHEMA


def check_policy(selection):
    if "semantic_unit_policy" in selection and selection["semantic_unit_policy"] != POLICY:
        raise ExportInputError("病理/IHC 字段必须保留标记、评分及标本/检测归属，不能只导出无归属数值。")


def capture_context(fields):
    """Freeze the actual effective closure, including unselected member heads."""
    contexts = {}
    for field in fields:
        if not is_pathology(field):
            continue
        if not field.get("usable") or field.get("context_state") != "RESOLVED" or not field.get("source_valid"):
            raise ExportInputError("部分病理/IHC 字段的当前来源或关联尚未核对。")
        contexts[field["id"]] = deepcopy({
            "report_id": field["report_id"], "entity_key": field["entity_key"], "field_key": field["field_key"],
            "source_token": field["current_source_token"], "revision_number": field["revision_number"],
            "revision_id": field["revision_id"], "created_by": field.get("created_by"),
            "revision_author_tuples": field.get("revision_author_tuples", []),
            "snapshot": field["context_snapshot"], "source_role": field["content"]["source_role"],
            "semantic_qualifiers": field["current_semantic_qualifiers"],
        })
    return contexts


def choice_texts(reports):
    fields = [field for report in reports for field in report["fields"] if field["usable"]]
    return {row["id"]: row["content"]["text"] for row in project_fields(fields, capture_context(fields), {})}


def selection_stamp(reports):
    from apps.facts.readmodels import digest

    current = [row for row in reports if row.get("schema_version") == SCHEMA]
    return digest(current) if current else None


def selection_unchanged(patient, stamp):
    if stamp is None:
        return True
    from apps.facts.clinical_readmodels import report_material

    return selection_stamp(report_material(patient)) == stamp


def _context(row, contexts):
    context = contexts.get(row["id"])
    if (not context or context.get("report_id") != row["report_id"] or context.get("entity_key") != row["entity_key"]
            or context.get("field_key") != row["field_key"] or context.get("revision_id") != row["revision_id"]
            or context.get("revision_number") != row["revision_number"]
            or context.get("snapshot", {}).get("context_version") != CONTEXT
            or context["snapshot"].get("binding_state") != "RESOLVED"):
        raise ExportInputError("病理/IHC 字段缺少本次已核对的来源关联，不能按普通数值输出。")
    return context


def _scope_keys(row, context):
    targets = {binding["role"]: binding["target_entity_key"] for binding in context["snapshot"]["bindings"]
               if binding["state"] == "BOUND"}
    for role in FIELDS[row["field_key"]].roles:
        if role not in targets:
            raise ExportInputError("病理/IHC 字段存在未关联的标本、检测或标记。")
    if row["field_key"] == "specimen.identity":
        targets["SPECIMEN"] = row["entity_key"]
    elif row["field_key"] == "assay.identity":
        targets["ASSAY"] = row["entity_key"]
    return {role: (row["report_id"], targets[role]) for role in ("SPECIMEN", "ASSAY") if targets.get(role)}


def _value(row):
    value = deepcopy(row["content"]["value"])
    kind = FIELDS[row["field_key"]].value_type
    # A numeric raw clause can contain a different field or a full header.
    # Text/identity/method values are themselves explicitly selected semantics.
    if kind in {"IHC_SCORE", "PATHOLOGY_DIMENSIONS", "NODE_COUNTS", "MARKER"}:
        value.pop("raw", None)
    if kind == "NODE_COUNTS":
        for group in value["groups"]:
            group.pop("raw", None)
    return value


def _text(key, value, bundle):
    kind = FIELDS[key].value_type
    if kind == "PATHOLOGY_DIMENSIONS":
        axes = {"LONG": "长", "SHORT": "短", "AP": "前后", "TRANSVERSE": "横径", "CC": "上下"}
        text = ("约" if value["approximate"] else "") + " × ".join(
            component["value"] + (component["unit"] or "（单位未注明）")
            + ("（" + axes.get(component["axis"], component["axis"]) + "）" if component["axis"] else "")
            for component in value["components"])
        text += "；" + {"SPECIMEN": "整体标本", "TUMOR": "肿瘤", "UNKNOWN": "测量对象未注明"}[value["measurement_object"]]
        text += "；" + {"CURRENT": "本次原文", "HISTORICAL": "历史原文", "UNKNOWN": "时间角色未注明"}[value["measurement_role"]]
    elif kind == "NODE_COUNTS":
        text = "；".join(group["label"] + "：检出 " + (group["sampled"] if group["sampled"] is not None else "未提供")
                         + "，阳性 " + (group["positive"] if group["positive"] is not None else "未提供") for group in value["groups"])
    else:
        text = display_value(key, value)
    if key in {"ihc.score", "ihc.result"}:
        text = bundle["marker"]["label"] + " " + text
    else:
        text = FIELDS[key].label + "：" + text
    if value.get("assertion"):
        text += "；" + ASSERTION_LABELS[value["assertion"]]
    aliases = [bundle[role]["label"] for role in ("specimen_scope", "assay_scope") if role in bundle]
    if aliases:
        text += "；" + " / ".join(aliases)
    if key in {"ihc.score", "ihc.result"}:
        text += "；" + ("检测条件另见本次选定字段" if bundle["assay_conditions"] == "SEE_SELECTED_FIELDS_NOT_COMPARABLE"
                       else "检测条件未纳入") + "，不可据此判断可比"
    return text


def project_fields(fields, contexts, selection):
    """Project trusted private rows, or reselect an already frozen snapshot."""
    chosen = [row for row in fields if is_pathology(row)]
    if not chosen:
        return deepcopy(fields)
    check_policy(selection)
    bindings = {row["id"]: _scope_keys(row, _context(row, contexts)) for row in chosen}
    namespace = uuid.uuid4()
    aliases = {}
    for role, label in (("SPECIMEN", "选定标本"), ("ASSAY", "选定检测")):
        keys = sorted({scopes[role] for scopes in bindings.values() if role in scopes})
        aliases[role] = {key: {"token": str(uuid.uuid5(namespace, json_identity(role, key))), "label": f"{label} {index}"}
                         for index, key in enumerate(keys, 1)}
    selected_conditions = {bindings[row["id"]]["ASSAY"] for row in chosen
                           if row["field_key"] in {"assay.method", "assay.antibody"}}
    output = []
    for original in fields:
        row = deepcopy(original)
        if not is_pathology(row):
            output.append(row)
            continue
        context = _context(row, contexts)
        if context["source_role"] not in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"}:
            raise ExportInputError("历史、对照或未明确来源不能作为本次病理/IHC 结果输出。")
        scopes = bindings[row["id"]]
        bundle = {"policy": POLICY, "source_role": context["source_role"], "binding_state": "RESOLVED"}
        for role, name in (("SPECIMEN", "specimen_scope"), ("ASSAY", "assay_scope")):
            if role in scopes:
                bundle[name] = deepcopy(aliases[role][scopes[role]])
        value = _value(row)
        if row["field_key"] in {"ihc.score", "ihc.result"}:
            qualifiers = context["semantic_qualifiers"]
            marker = qualifiers.get("marker")
            if (context["source_role"] != "CURRENT_RESULT" or not isinstance(marker, dict) or not marker.get("label")
                    or qualifiers.get("binding_state") != "RESOLVED" or not all(role in scopes for role in ("SPECIMEN", "ASSAY"))):
                raise ExportInputError("评分或结果缺少已核对的标记和检测/标本归属。")
            bundle.update(marker={key: deepcopy(marker[key]) for key in ("code", "label")},
                          qualitative_result=value["assertion"],
                          assay_conditions="SEE_SELECTED_FIELDS_NOT_COMPARABLE" if scopes["ASSAY"] in selected_conditions
                          else "NOT_INCLUDED_NOT_COMPARABLE")
        row["content"] = {
            "category": row["category"], "schema_version": SCHEMA, "field_key": row["field_key"],
            "value_type": FIELDS[row["field_key"]].value_type, "result_type": row["content"]["result_type"],
            "value": value, "text": _text(row["field_key"], value, bundle), "semantic_qualifiers": bundle,
            "source_context_omitted": True, "source_role": context["source_role"],
            "date": None, "date_raw": "", "date_precision": "UNKNOWN", "dates": [], "institution": "",
            "record_date": {"value": None, "raw": "", "precision": "UNKNOWN"}, "limitations": [],
        }
        row["source"] = {key: deepcopy(value) for key, value in row["source"].items() if key in SOURCE_KEYS}
        row["source"]["raw_text"] = ""
        output.append(row)
    validate_portable_fields(output)
    return output


def json_identity(role, key):
    # Length-prefixed components avoid alias collisions from arbitrary labels.
    return role + "".join(f"{len(part)}:{part}" for part in key)


def project_reports(reports, fields, sources):
    pathology_reports = {row["report_id"] for row in fields if is_pathology(row)}
    result = []
    for report in reports:
        if report["id"] not in pathology_reports:
            result.append(deepcopy(report))
            continue
        row = {key: deepcopy(value) for key, value in report.items() if key in REPORT_KEYS}
        row.update(title="病理/IHC（本次选定字段）", spans=[],
                   pages=sorted({source["page"] for source in sources if source["report_id"] == report["id"]}))
        result.append(row)
    return result


def project_documents(documents, fields):
    selected = {row["source"]["document_id"] for row in fields if is_pathology(row)}
    result = deepcopy(documents)
    for index, document in enumerate(result, 1):
        if document["id"] not in selected:
            continue
        extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}.get(document["content_type"], "")
        document.update(filename=f"selected-source-{index:02d}{extension}", institution="", date=None,
                        date_raw="", date_precision="UNKNOWN")
    return result


def redact_sources(sources, fields):
    selected = {row["id"] for row in fields if is_pathology(row)}
    for source in sources:
        if source["fact_id"] in selected:
            source.update(raw_text="", start_offset=None, end_offset=None)


def validate_portable_fields(rows):
    """Old fields remain old; new-mode fields cannot fall back to bare values."""
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue  # Preserve existing legacy-reader behavior.
            key = row.get("field_key")
            schema = row.get("schema_version")
            definition = FIELDS.get(key)
            if not (isinstance(schema, str) and schema.startswith("PATHOLOGY_")
                    or definition and definition.version == SCHEMA):
                continue
            content = row["content"]
            bundle = content["semantic_qualifiers"]
            if (schema != SCHEMA or content["schema_version"] != SCHEMA or not definition or definition.version != SCHEMA
                    or content["field_key"] != key or content["value_type"] != definition.value_type
                    or bundle["policy"] != POLICY or bundle["binding_state"] != "RESOLVED"
                    or content["source_role"] not in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"}
                    or content["source_role"] != bundle["source_role"]):
                raise ValueError
            required = set(definition.roles) & {"SPECIMEN", "ASSAY"}
            if key == "specimen.identity":
                required.add("SPECIMEN")
            if key == "assay.identity":
                required.add("ASSAY")
            for role in required:
                scope = bundle[role.lower() + "_scope"]
                if not isinstance(scope["label"], str) or not scope["label"]:
                    raise ValueError
                uuid.UUID(scope["token"])
            value = deepcopy(content["value"])
            if definition.value_type in {"IHC_SCORE", "PATHOLOGY_DIMENSIONS", "NODE_COUNTS", "MARKER"}:
                value["raw"] = "selected semantic value"
            if definition.value_type == "NODE_COUNTS":
                for group in value["groups"]:
                    group["raw"] = "selected semantic group"
            validate_value(key, value)
            if key in {"ihc.score", "ihc.result"}:
                marker = bundle["marker"]
                validate_value("ihc.marker", {"code": marker["code"], "label": marker["label"], "raw": marker["label"]})
                if (bundle["source_role"] != "CURRENT_RESULT" or bundle["qualitative_result"] != value["assertion"]
                        or bundle["assay_conditions"] not in {"NOT_INCLUDED_NOT_COMPARABLE", "SEE_SELECTED_FIELDS_NOT_COMPARABLE"}):
                    raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise ExportInputError("病理/IHC 字段格式或必需限定缺失，不能作为无归属数值读取。") from None
