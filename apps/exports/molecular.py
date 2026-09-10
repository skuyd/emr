"""Selected molecular meaning. Actual validation material never leaves the snapshot."""
from copy import deepcopy
import uuid

from django.core.exceptions import ValidationError

from apps.facts.clinical_schema import FIELDS, validate_value
from apps.facts.molecular_schema import SCHEMA, CONTEXT, ASSAY_MEMBERS
from apps.cloud_imaging.projection import project_default_snapshot

from .errors import ExportInputError
from . import pathology

POLICY = "MOLECULAR_SEMANTIC_UNIT_V1"
PRIVATE_CONTEXT = "molecular_validation_context"
OMITTED = "NOT_INCLUDED_NOT_COMPARABLE"
NO_ASSERTION = "AS_REPORTED_NO_POSITIVITY_INFERRED"
COMPOSITE = {"VARIANT_IDENTITY", "ALLELE_FRACTION", "COPY_NUMBER", "MSI", "TMB", "PANEL_SIZE", "DRUG_GROUP", "DRUG_LEVEL"}
ROW_KEYS = {"id", "report_id", "field_key", "field_label", "schema_version", "origin", "category", "category_label",
            "status", "revision_number", "revision_id", "conflict", "content", "source"}
CONTENT_KEYS = {"category", "schema_version", "field_key", "value_type", "result_type", "value", "text",
                "molecular_semantic_unit", "source_context_omitted", "source_role", "date", "date_raw", "date_precision",
                "dates", "institution", "record_date", "limitations"}
UNIT_KEYS = {"policy", "source_role", "binding_state", "specimen_scope", "assay_scope", "variants", "drug_evidence",
             "reported_assertion", "assay_conditions", "specimen_name", "assay_name"}
DRUG_KEYS = {"alias", "report_recorded_only", "drugs", "association", "statement", "direction", "context", "level", "report_date"}


def is_molecular(row):
    return row.get("schema_version") == SCHEMA


def check_policy(selection):
    if "molecular_semantic_unit_policy" in selection and selection["molecular_semantic_unit_policy"] != POLICY:
        raise ExportInputError("分子结果须保留完整变异身份、检测范围及报告药物依据，不能只携带裸值。")


def capture_context(fields, material):
    """Freeze real effective values as well as source, author and revision heads."""
    reports = {report["id"]: report for report in material}
    contexts = {"selected": {}, "reports": {}}
    for row in fields:
        if not is_molecular(row):
            continue
        _usable(row)
        contexts["selected"][row["id"]] = deepcopy(row)
        if row["report_id"] not in contexts["reports"]:
            contexts["reports"][row["report_id"]] = deepcopy(reports[row["report_id"]])
    return contexts if contexts["selected"] else {}


def _usable(row):
    if not row.get("usable") or not row.get("source_valid") or row.get("context_state") != "RESOLVED":
        raise ExportInputError("分子结果的原件、身份或当前核对状态已变化。")


def _context(row, contexts):
    original = contexts.get("selected", {}).get(row["id"], {})
    if (any(original.get(k) != row.get(k) for k in ("id", "field_key", "report_id", "revision_id", "revision_number"))
            or original.get("context_snapshot", {}).get("context_version") != CONTEXT
            or original.get("context_snapshot", {}).get("binding_state") != "RESOLVED"):
        raise ExportInputError("分子结果缺少当前真实身份闭包，不能回退成普通字段。")
    _usable(original)
    return {"row": original, "report": contexts["reports"][row["report_id"]]}


def _bindings(row):
    result = {}
    for binding in row["context_snapshot"]["bindings"]:
        if binding["state"] != "BOUND":
            raise ExportInputError("未关联的检测、标本或变异不能作为结果单元输出。")
        result.setdefault(binding["role"], []).append(binding["target_fact_id"])
    return result


def _value(key, value):
    result = deepcopy(value)
    if FIELDS[key].value_type in COMPOSITE:
        result.pop("raw", None)
    return project_default_snapshot(result)


def _validated_value(key, value):
    result = pathology._validation_value(value)
    if FIELDS[key].value_type in COMPOSITE:
        if "raw" in result:
            raise ValueError("Composite original clause is private")
        result["raw"] = "detached validation only"
    validate_value(key, result)
    return result


def _identity(value):
    result = _value("variant.identity", value)
    checked = _validated_value("variant.identity", result)
    if checked["status"] != "COMPLETE" or checked["scope"] == "UNKNOWN":
        raise ExportInputError("变异身份或来源范围未明确，不能作为选定结果输出。")
    return result


def _member(rows, key, *, required=False):
    candidates = [r for r in rows if r["field_key"] == key]
    if not candidates:
        if required:
            raise ExportInputError("报告药物依据缺少已核对的完整语义。")
        return {"state": "NOT_STATED", "value": None}
    for row in candidates:
        _usable(row)
    values = [_value(key, row["content"]["value"]) for row in candidates]
    if any(value != values[0] for value in values[1:]):
        raise ExportInputError("报告药物依据或日期存在冲突，不能选择一个值代替。")
    return {"state": "REPORTED", "value": values[0]}


def _drug(row, context, bindings, alias):
    material = context["report"]["fields"]
    members = [r for r in material if r["entity_key"] == row["entity_key"] and r["field_key"].startswith("drug_evidence.")]
    drugs = _member(members, "drug_evidence.drugs", required=True)["value"]
    assay_id, = bindings["ASSAY"]
    assay = next(r for r in material if r["id"] == assay_id)
    dates = [r for r in material if r["entity_key"] == assay["entity_key"] and r["field_key"] == "assay.report_date"]
    # Shared dates retain their published IHC value/precision shape. raw_value
    # can be an entire manual transcription, so it stays in private material.
    date = _member(dates, "assay.report_date")
    association = row["content"]["entity_context"]["association"]
    if association["state"] != "EXPLICIT":
        raise ExportInputError("报告药物依据的原变异关联未明确。")
    return {"alias": alias("DRUG_EVIDENCE", row["id"] if row["field_key"] == "drug_evidence.drugs" else bindings["DRUG_EVIDENCE"][0]),
            "report_recorded_only": True, "drugs": drugs,
            # Preserve the actual declared target set and order, not a whole
            # association clause that may also contain private adjacent cells.
            "association": {"state": "EXPLICIT", "variants": [alias("VARIANT", identity) for identity in bindings["VARIANT"]],
                            "interpretation": "AS_REPORTED_NO_LOGIC_INFERRED"},
            **{name: _member(members, "drug_evidence." + name) for name in ("statement", "direction", "context", "level")},
            "report_date": date}


def _component_text(value):
    if isinstance(value, dict):
        if "state" in value and value["state"] in {"UNKNOWN", "NOT_PRINTED", "NOT_STATED"}:
            return {"UNKNOWN": "未知", "NOT_PRINTED": "原件未印", "NOT_STATED": "报告未说明"}[value["state"]]
        return " / ".join(_component_text(v) for k, v in sorted(value.items()) if k not in {"status", "assertion", "external_access_omitted"})
    if isinstance(value, list):
        return "、".join(_component_text(v) for v in value)
    if value is None:
        return "未知"
    return {"UNKNOWN": "未知", "NOT_PRINTED": "原件未印", "PRINTED": "原件明确记载", "SOMATIC": "体细胞",
            "GERMLINE": "胚系", "FIVE_TO_THREE": "原文5′至3′顺序", "NOT_STATED": "报告未说明"}.get(str(value), str(value))


def _component(value):
    if value["state"] != "PRINTED":
        return {"NOT_PRINTED": "原件未印", "UNKNOWN": "未知"}[value["state"]]
    return "、".join(value["values"]) if "values" in value else value["raw"]


def _identity_text(value):
    kinds = {"SMALL_VARIANT": "小变异", "COPY_NUMBER": "拷贝数变异", "FUSION": "融合"}
    pieces = ["类型：" + kinds[value["kind"]], "范围：" + {"SOMATIC": "体细胞", "GERMLINE": "胚系", "UNKNOWN": "未知"}[value["scope"]]]
    if value["kind"] == "COPY_NUMBER":
        pieces.extend(["基因：" + _component(value["gene"]), "变化：" + _component(value["change"])])
    else:
        pieces.append("原表达：" + _component(value["expression"]))
        if value["kind"] == "SMALL_VARIANT":
            pieces.append("基因：" + _component(value["gene"]))
            for name, label in (("coding", "编码位点"), ("protein", "蛋白位点"), ("codons", "密码子"), ("transcripts", "转录本"), ("locations", "参考位置")):
                pieces.append(label + "：" + _component(value[name]))
        else:
            pieces.append("原文5′至3′顺序：" + " → ".join(_component(p["gene"]) + "（转录本：" + _component(p["transcripts"]) + "；断点：" + _component(p["breakpoints"]) + "）" for p in value["partners"]))
    return "；".join(pieces)


def _value_text(key, value):
    kind = FIELDS[key].value_type
    if kind in {"ALLELE_FRACTION", "COPY_NUMBER", "MSI", "TMB", "PANEL_SIZE"}:
        if value["status"] == "UNRESOLVED":
            text = "原数值未能解释"
        else:
            text = ("约 " if value["approximate"] else "") + {"EQ": "", "LT": "<", "LE": "≤", "GT": ">", "GE": "≥", "RANGE": ""}[value["comparator"]]
            text += " 至 ".join(value["values"])
        text += " " + value["unit"] if value["unit"] else ("（单位未印刷）" if value["unit_state"] == "NOT_PRINTED" else "（单位未知）")
        if value["assertion"] == "UNCERTAIN": text += "（原文不确定）"
        if kind == "PANEL_SIZE": text += "；计数对象：" + _component(value["count_object"])
        return text
    if kind == "VARIANT_IDENTITY": return _identity_text(value)
    if kind == "COMPONENT": return _component(value)
    if kind == "TEXT": return value["text"]
    if kind == "CODED": return value["raw"] + ("（未分类原文）" if value["code"] == "UNKNOWN" else "")
    if kind == "DRUG_GROUP":
        return "、".join(value["names"]) + "（" + {"SINGLE": "原文单药", "AND": "原文组合", "OR": "原文或关系", "ALTERNATIVE": "原文备选", "UNKNOWN": "组合关系未知"}[value["relation"]] + "）"
    if kind == "DRUG_LEVEL": return "等级：" + _component(value["grade"]) + "；等级体系：" + _component(value["system"])
    if kind == "MOLECULAR_NEGATIVE":
        scope = value["scope"]
        return value["text"] + "；检测范围：" + (scope["raw"] or "未知") + "；检测种类：" + "、".join(x["raw"] for x in scope["detection_kinds"]) + "；原目标：" + "、".join(scope["targets"]) + "；原限制：" + ("、".join(scope["limitations"]) or "未另行说明")
    if key == "assay.report_date":
        return (value.get("raw") or value["value"] or "未知") + "（" + {"DAY": "日", "MONTH": "月", "YEAR": "年", "UNKNOWN": "精度未知"}[value["precision"]] + "）"
    return _component_text(value)


def _text(key, value, unit):
    text = FIELDS[key].label + "：" + _value_text(key, value)
    for variant in unit["variants"]:
        if key != "variant.identity":
            text += "；" + variant["alias"]["label"] + " 完整身份：" + _identity_text(variant["identity"])
    drug = unit["drug_evidence"]
    if drug:
        text += "；仅为报告记载，不是治疗建议；药物/组合：" + _value_text("drug_evidence.drugs", drug["drugs"])
        text += "；原关联（按报告顺序，不推断获益逻辑）：" + "、".join(v["label"] for v in drug["association"]["variants"])
        for name, label in (("statement", "依据原文"), ("direction", "依据方向"), ("context", "依据上下文"), ("level", "等级及体系"), ("report_date", "报告日期")):
            meaning = "报告未说明" if drug[name]["state"] == "NOT_STATED" else _value_text("assay.report_date" if name == "report_date" else "drug_evidence." + name, drug[name]["value"])
            text += "；" + label + "：" + meaning
    text += "；" + pathology.ASSERTION_LABELS[unit["reported_assertion"]["code"]]
    text += "；" + unit["specimen_scope"]["label"] + " / " + unit["assay_scope"]["label"]
    return text + "；未选标本名称和检测条件未纳入，不可据此判断可比"


def selection_aliases(fields, pathology_contexts, molecular_contexts):
    """One alias space for shared IHC/date fields and molecular fields."""
    keys = set()
    for row in fields:
        if is_molecular(row):
            original = _context(row, molecular_contexts)["row"]
            for role, ids in _bindings(original).items():
                keys.update((role, identity) for identity in ids)
            if row["field_key"] == "variant.identity":
                keys.add(("VARIANT", row["id"]))
            elif row["field_key"] == "drug_evidence.drugs":
                keys.add(("DRUG_EVIDENCE", row["id"]))
        elif pathology.is_pathology(row):
            for role, (_, identity) in pathology._scope_keys(row, pathology._context(row, pathology_contexts)).items():
                keys.add((role, identity))
    namespace = uuid.uuid4()
    output = {}
    for role in ("SPECIMEN", "ASSAY", "VARIANT", "DRUG_EVIDENCE"):
        label = {"SPECIMEN": "选定标本", "ASSAY": "选定检测", "VARIANT": "选定变异", "DRUG_EVIDENCE": "报告药物依据"}[role]
        for index, (_, identity) in enumerate(sorted(k for k in keys if k[0] == role), 1):
            output[role, identity] = {"token": str(uuid.uuid5(namespace, role + ":" + identity)), "label": f"{label} {index}"}
    return output


def project_fields(fields, contexts, selection, *, scope_aliases=None):
    chosen = [row for row in fields if is_molecular(row)]
    if not chosen:
        return deepcopy(fields)
    check_policy(selection)
    namespace = uuid.uuid4()
    aliases = {}
    def alias(role, identity):
        key = role, identity
        if scope_aliases is not None:
            return deepcopy(scope_aliases[key])
        if key not in aliases:
            n = 1 + sum(r == role for r, _ in aliases)
            label = {"SPECIMEN": "选定标本", "ASSAY": "选定检测", "VARIANT": "选定变异", "DRUG_EVIDENCE": "报告药物依据"}[role]
            aliases[key] = {"token": str(uuid.uuid5(namespace, role + ":" + identity)), "label": f"{label} {n}"}
        return deepcopy(aliases[key])
    output = []
    for public in fields:
        if not is_molecular(public):
            output.append(deepcopy(public))
            continue
        context = _context(public, contexts)
        original = context["row"]
        bindings = _bindings(original)
        by_id = {r["id"]: r for r in context["report"]["fields"]}
        for role in ("SPECIMEN", "ASSAY"):
            if len(bindings.get(role, [])) != 1:
                raise ExportInputError("分子结果必须属于明确的标本和检测。")
            _usable(by_id[bindings[role][0]])
        ids = [original["id"]] if original["field_key"] == "variant.identity" else bindings.get("VARIANT", [])
        variants = []
        for identity in ids:
            target = by_id[identity]
            _usable(target)
            if target["field_key"] != "variant.identity":
                raise ExportInputError("变异归属不是实际完整身份锚。")
            variants.append({"alias": alias("VARIANT", identity), "identity": _identity(target["content"]["value"])})
        assertion = original["content"]["reported_assertion"]
        unit = {"policy": POLICY, "source_role": original["content"]["source_role"], "binding_state": "RESOLVED",
                "specimen_scope": alias("SPECIMEN", bindings["SPECIMEN"][0]), "assay_scope": alias("ASSAY", bindings["ASSAY"][0]),
                "variants": variants, "drug_evidence": None,
                "reported_assertion": {"code": assertion["code"], "raw": project_default_snapshot(assertion["raw"])},
                "assay_conditions": OMITTED, "specimen_name": OMITTED, "assay_name": OMITTED}
        key = original["field_key"]
        if key.startswith("drug_evidence."):
            unit["drug_evidence"] = _drug(original, context, bindings, alias)
        value = _value(key, original["content"]["value"])
        row = {key: deepcopy(value) for key, value in public.items() if key in ROW_KEYS}
        row["content"] = {"category": row["category"], "schema_version": SCHEMA, "field_key": key,
            "value_type": FIELDS[key].value_type, "result_type": original["content"]["result_type"],
            "value": value, "text": _text(key, value, unit), "molecular_semantic_unit": unit,
            "source_context_omitted": True, "source_role": unit["source_role"], "date": None, "date_raw": "",
            "date_precision": "UNKNOWN", "dates": [], "institution": "", "record_date": {"value": None, "raw": "", "precision": "UNKNOWN"}, "limitations": []}
        row["source"] = {key: deepcopy(value) for key, value in public["source"].items() if key in pathology.SOURCE_KEYS}
        row["source"]["raw_text"] = ""
        output.append(row)
    validate_portable_fields(output, {"molecular_semantic_unit_policy": POLICY})
    return output


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid semantic shape")


def _alias(value):
    _shape(value, {"token", "label"})
    uuid.UUID(value["token"])
    if not isinstance(value["label"], str) or not value["label"] or len(value["label"]) > 80:
        raise ValueError


def _state_value(item, key):
    _shape(item, {"state", "value"})
    if item["state"] == "NOT_STATED":
        if item["value"] is not None:
            raise ValueError
    elif item["state"] == "REPORTED":
        if key == "assay.report_date":
            validate_value(key, pathology._validation_value(item["value"]))
        else:
            _validated_value(key, item["value"])
    else:
        raise ValueError


def validate_portable_fields(rows, scope):
    """Validate declared meaning, never certify a file's source truth offline."""
    identities = {}
    drug_units = {}
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            key, schema = row.get("field_key"), row.get("schema_version")
            definition = FIELDS.get(key)
            declared = row.get("content", {})
            nested = isinstance(declared, dict) and ("molecular_semantic_unit" in declared or
                     isinstance(declared.get("schema_version"), str) and declared["schema_version"].startswith("MOLECULAR_"))
            if not (nested or isinstance(schema, str) and schema.startswith("MOLECULAR_") or definition and definition.version == SCHEMA):
                continue
            if schema != SCHEMA or not definition or definition.version != SCHEMA or scope.get("molecular_semantic_unit_policy") != POLICY:
                raise ValueError
            if set(row) - ROW_KEYS:
                raise ValueError
            content = row["content"]
            _shape(content, CONTENT_KEYS)
            unit = content["molecular_semantic_unit"]
            _shape(unit, UNIT_KEYS)
            if (content["schema_version"] != SCHEMA or content["field_key"] != key or content["value_type"] != definition.value_type
                    or content["result_type"] != "SOURCE_REPORTED"
                    or content["source_context_omitted"] is not True or unit["policy"] != POLICY or unit["binding_state"] != "RESOLVED"
                    or unit["source_role"] != content["source_role"] or unit["source_role"] not in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA", "REPORT_DRUG_EVIDENCE"}
                    or any(unit[name] != OMITTED for name in ("assay_conditions", "specimen_name", "assay_name"))):
                raise ValueError
            allowed_roles = {"CURRENT_RESULT", "REPORT_DRUG_EVIDENCE"} if key.startswith("drug_evidence.") else ({"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"} if key in ASSAY_MEMBERS else {"CURRENT_RESULT"})
            if unit["source_role"] not in allowed_roles or content["category"] != row["category"] or row["category"] != "MOLECULAR":
                raise ValueError
            if (set(row["source"]) - (pathology.SOURCE_KEYS | {"raw_text"}) or row["source"].get("raw_text", "") != ""
                    or content["date"] is not None or content["date_raw"] != "" or content["date_precision"] != "UNKNOWN"
                    or content["dates"] or content["institution"] or content["limitations"]
                    or content["record_date"] != {"value": None, "raw": "", "precision": "UNKNOWN"}):
                raise ValueError
            for role in ("specimen_scope", "assay_scope"):
                _alias(unit[role])
            _validated_value(key, content["value"])
            if key == "assay.negative_statement" and content["value"]["scope"]["state"] != "EXPLICIT":
                raise ValueError
            assertion = unit["reported_assertion"]
            _shape(assertion, {"code", "raw"})
            if assertion["code"] == NO_ASSERTION:
                if assertion["raw"] is not None:
                    raise ValueError
            elif assertion["code"] not in pathology.ASSERTION_LABELS or not isinstance(assertion["raw"], str) or not assertion["raw"].strip():
                raise ValueError
            else:
                from apps.facts.molecular_source_codes import validate_assertion_code
                validate_assertion_code(assertion["code"], assertion["raw"])
            variants = unit["variants"]
            if not isinstance(variants, list) or len(variants) > 8:
                raise ValueError
            if key.startswith("variant.") and len(variants) != 1 or key.startswith("drug_evidence.") and not variants:
                raise ValueError
            if not key.startswith(("variant.", "drug_evidence.")) and variants:
                raise ValueError
            seen = set()
            for item in variants:
                _shape(item, {"alias", "identity"}); _alias(item["alias"])
                identity = item["identity"]
                _identity(identity)
                token = item["alias"]["token"]
                if token in seen:
                    raise ValueError
                seen.add(token)
                descriptor = {"identity": identity, "specimen_scope": unit["specimen_scope"], "assay_scope": unit["assay_scope"]}
                if token in identities and identities[token] != descriptor:
                    raise ValueError
                identities[token] = descriptor
            if key == "variant.identity" and content["value"] != variants[0]["identity"]:
                raise ValueError
            from apps.facts.molecular_schema import COMPONENT_KEYS
            from apps.facts.molecular_context import component_agrees
            if key in COMPONENT_KEYS and not component_agrees(key, content["value"], variants[0]["identity"]):
                raise ValueError
            drug = unit["drug_evidence"]
            if key.startswith("drug_evidence."):
                _shape(drug, DRUG_KEYS); _alias(drug["alias"])
                if drug["report_recorded_only"] is not True:
                    raise ValueError
                _validated_value("drug_evidence.drugs", drug["drugs"])
                _shape(drug["association"], {"state", "variants", "interpretation"})
                if (drug["association"]["state"] != "EXPLICIT" or drug["association"]["interpretation"] != "AS_REPORTED_NO_LOGIC_INFERRED"
                        or drug["association"]["variants"] != [v["alias"] for v in variants]):
                    raise ValueError
                for name in ("statement", "direction", "context", "level"):
                    _state_value(drug[name], "drug_evidence." + name)
                _state_value(drug["report_date"], "assay.report_date")
                selected = drug["drugs"] if key.endswith(".drugs") else drug[key.split(".")[1]]["value"]
                if selected != content["value"]:
                    raise ValueError
                token = drug["alias"]["token"]
                descriptor = {"drug": drug, "specimen_scope": unit["specimen_scope"], "assay_scope": unit["assay_scope"]}
                if token in drug_units and drug_units[token] != descriptor:
                    raise ValueError
                drug_units[token] = descriptor
            elif drug is not None:
                raise ValueError
            if content["text"] != _text(key, content["value"], unit):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise ExportInputError("分子字段的模式、选择策略、完整身份或省略声明无效，不能读取为无归属裸值。") from None


def project_reports(reports, fields, sources):
    selected = {row["report_id"] for row in fields if is_molecular(row)}
    selected.update(row["id"] for row in reports if row.get("routing_kind") == "MOLECULAR")
    result = []
    for original in reports:
        if original["id"] not in selected:
            result.append(deepcopy(original)); continue
        row = {k: deepcopy(v) for k, v in original.items() if k in pathology.REPORT_KEYS}
        row.update(title="分子检测（本次选定字段）", spans=[], pages=sorted({s["page"] for s in sources if s["report_id"] == row["id"]}))
        result.append(row)
    return result


def project_documents(documents, fields):
    tagged = [{**row, "schema_version": pathology.SCHEMA} for row in fields if is_molecular(row)]
    return pathology.project_documents(documents, tagged)


def redact_sources(sources, fields):
    selected = {row["id"] for row in fields if is_molecular(row)}
    for source in sources:
        if source["fact_id"] in selected:
            source.update(raw_text="", start_offset=None, end_offset=None)


def choice_texts(reports):
    fields = [f for r in reports for f in r["fields"] if f["usable"]]
    if not any(is_molecular(row) for row in fields):
        return {}
    contexts = capture_context(fields, reports)
    aliases = selection_aliases(fields, pathology.capture_context(fields), contexts)
    labels = {}
    for row in fields:
        if not is_molecular(row):
            continue
        try:
            labels[row["id"]] = project_fields([row], contexts, {}, scope_aliases=aliases)[0]["content"]["text"]
            bindings = _bindings(row)
            originals = {f["id"]: f for r in reports for f in r["fields"]}
            names = [originals[bindings[role][0]]["content"]["value"]["label"] for role in ("SPECIMEN", "ASSAY")]
            labels[row["id"]] += "（原件核对归属：" + project_default_snapshot(" / ".join(names)) + "）"
        except ExportInputError:
            labels[row["id"]] = "必要身份或报告药物语义尚未完整核对，暂不能输出"
    return labels


def sharing_scope_current(share, snapshot):
    if not any(is_molecular(row) for row in snapshot.get("clinical_fields", [])):
        return True
    validate_portable_fields(snapshot["clinical_fields"], snapshot["selection"])
    return all(share.scope.get(k) == snapshot["selection"].get(k)
               for k in ("sections", "report_ids", "clinical_field_ids", "molecular_semantic_unit_policy"))


def selection_stamp(reports):
    from apps.facts.readmodels import digest
    current = [r for r in reports if r.get("routing_kind") == "MOLECULAR"]
    return digest(current) if current else None


def selection_unchanged(patient, stamp):
    if stamp is None:
        return True
    from apps.facts.clinical_readmodels import report_material
    return selection_stamp(report_material(patient)) == stamp
