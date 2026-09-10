"""Molecular graph policy; existing IHC graphs keep their original member set."""
from django.core.exceptions import ValidationError

from .clinical_schema import FIELDS
from .molecular_schema import ASSAY_MEMBERS, COMPONENT_KEYS, DRUG_MEMBERS, SCHEMA, TARGET_KEYS


def indexed_bindings(rows):
    # Repeated VARIANT roles are an ordered source set, never dict-overwritten.
    result, count = {}, 0
    for row in rows:
        role = row["role"]
        if role == "VARIANT":
            role = f"VARIANT:{count}"
            count += 1
        result[role] = row
    return result


def _members(resolver, fact, bindings):
    rank = FIELDS[fact.field_key].rank
    if rank < 30:
        return []
    entities = {b["target_entity_key"] for b in bindings.values()
                if b["role"] in {"SPECIMEN", "ASSAY"} and b["state"] == "BOUND"}
    variants = {b["target_entity_key"] for b in bindings.values() if b["role"] == "VARIANT" and b["state"] == "BOUND"}
    drugs = {b["target_entity_key"] for b in bindings.values() if b["role"] == "DRUG_EVIDENCE" and b["state"] == "BOUND"}
    return sorted((f for f in resolver.fields.values() if (
        (f.entity_key in entities and f.field_key in ASSAY_MEMBERS)
        or (rank >= 40 and f.entity_key in variants and f.field_key in COMPONENT_KEYS)
        or (rank == 60 and f.entity_key in drugs and f.field_key in DRUG_MEMBERS)
    )), key=lambda f: str(f.pk))


def _proof(by_ordinal, ordinals):
    if not set(ordinals) <= by_ordinal.keys():
        raise ValidationError("关联依据不属于本字段的实际原文片段。")
    return [by_ordinal[i] for i in ordinals]


def _prove_target(fact, target, proof):
    from .clinical_context import _literal
    from .pathology_source import covers_positions, source_material

    if fact.origin == "AUTOMATIC":
        material = source_material(target)
        if material is None or not covers_positions(proof, material["value"]):
            raise ValidationError("分子自动关联须完整覆盖该目标自己的原始值位置。")
    else:
        raw = target.automatic_content["value"].get("raw", "")
        if not raw or _literal(raw) not in _literal("\n".join(p.raw_text for p in proof)):
            raise ValidationError("请转录同一原件中该标本、检测、完整变异或药物组的身份依据。")
        if target.field_key == "variant.identity":
            _prove_printed(target.automatic_content["value"], proof)


def _prove_printed(value, pieces):
    from .clinical_context import _literal

    text = _literal("\n".join(p.raw_text for p in pieces))

    def visit(item):
        if isinstance(item, dict):
            if item.get("state") == "PRINTED":
                for literal in item.get("values", [item.get("raw")]):
                    if not literal or _literal(literal) not in text:
                        raise ValidationError("已印身份组件必须完整来自本字段自己的原文片段。")
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
    visit(value)


def validate_negative_source(fact, content, *, own_pieces=None):
    """Scope cannot be widened by a typed value or by a later correction."""
    from .clinical_context import _literal
    from .pathology_source import source_material

    if fact.field_key != "assay.negative_statement":
        return
    if own_pieces is None:
        material = source_material(fact)
        own_pieces = material["value"] if material else list(fact.source_fragments.all())
    source = _literal("\n".join(p.raw_text for p in own_pieces))
    value, scope = content["value"], content["value"]["scope"]
    literals = [value["text"]]
    if scope["state"] == "EXPLICIT":
        literals += [scope["raw"], *[item["raw"] for item in scope["detection_kinds"]], *scope["targets"], *scope["limitations"]]
    if any(_literal(literal) not in source for literal in literals):
        raise ValidationError("检测范围、种类、目标和限制必须来自本字段自己的原文，不能扩大或借用其他来源。")


def links(resolver, fact, *, fragments=None):
    from .clinical_context import _binding_identity, _literal, has_context
    from .pathology_source import source_material

    bindings = resolver._bindings(fact)
    pieces = list(fact.source_fragments.all()) if fragments is None else list(fragments)
    material = source_material(fact, fragments=pieces)
    if fact.origin == "AUTOMATIC" and material is None:
        raise ValidationError("新分子自动候选必须声明自己的原值与标签位置。")
    if fact.field_key == "variant.identity":
        _prove_printed(fact.automatic_content["value"], material["value"] if material else pieces)
    validate_negative_source(fact, fact.automatic_content, own_pieces=material["value"] if material else pieces)
    by_ordinal = {p.ordinal: p for p in pieces}
    if len(by_ordinal) != len(pieces) or any(p.fact_id != fact.pk for p in pieces):
        raise ValidationError("分子依据须属于本字段不同的实际片段。")
    targets = {}
    for index, binding in bindings.items():
        proof = _proof(by_ordinal, binding["proof_fragment_ordinals"])
        if binding["state"] == "UNKNOWN":
            continue
        role = binding["role"]
        target = resolver.fields.get(binding["target_fact_id"])
        if (target is None or target.pk == fact.pk or target.field_key != TARGET_KEYS[role]
                or target.entity_key != binding["target_entity_key"] or not has_context(target)
                or target.document_id != fact.document_id or target.document.patient_id != fact.document.patient_id
                or target.parsing_version_id != fact.parsing_version_id
                or FIELDS[target.field_key].rank >= FIELDS[fact.field_key].rank):
            raise ValidationError("分子关联须指向同报告、同原件版本中更低层的明确身份。")
        kind = FIELDS[fact.field_key].entity_kind
        own_role = {"specimen": "SPECIMEN", "assay": "ASSAY", "variant": "VARIANT", "drug_evidence": "DRUG_EVIDENCE"}.get(kind)
        if role == own_role and target.entity_key != fact.entity_key:
            raise ValidationError("分子字段必须引用自己所在实体的身份锚。")
        _prove_target(fact, target, proof)
        targets[index] = target
        parents = resolver._bindings(target)
        for ancestor in {"SPECIMEN", "ASSAY"} & bindings.keys() & parents.keys():
            if _binding_identity(bindings[ancestor]) != _binding_identity(parents[ancestor]):
                raise ValidationError("字段的标本/检测与父锚矛盾，不能推断替换。")
        if role == "DRUG_EVIDENCE":
            own = [_binding_identity(b) for b in bindings.values() if b["role"] == "VARIANT"]
            parent = [_binding_identity(b) for b in parents.values() if b["role"] == "VARIANT"]
            if own != parent or fact.automatic_content["entity_context"]["association"]["raw"] != target.automatic_content["entity_context"]["association"]["raw"]:
                raise ValidationError("药物字段须保留该药物组完整且同序的原文变异关联集合。")
    context = fact.automatic_content["entity_context"]
    association = context["association"]
    if association and association["state"] == "EXPLICIT":
        proof = _proof(by_ordinal, association["proof_fragment_ordinals"])
        if _literal(association["raw"]) not in _literal("\n".join(p.raw_text for p in proof)):
            raise ValidationError("药物关联声明必须有本字段实际原文。")
        for index, target in targets.items():
            if bindings[index]["role"] == "VARIANT":
                _prove_target(fact, target, proof)
    assertion = fact.automatic_content["reported_assertion"]
    proof = _proof(by_ordinal, assertion["proof_fragment_ordinals"])
    if material and assertion["raw"] is not None:
        from .pathology_source import covers_positions
        if not covers_positions(material["value"], proof):
            raise ValidationError("明示结果须来自本字段自己的原值窗口，不能借用复制的关联锚。")
    if assertion["raw"] is not None and _literal(assertion["raw"]) not in _literal("\n".join(p.raw_text for p in proof)):
        raise ValidationError("明示检测结果必须有本字段实际原文，不能由数值推断。")
    return bindings, targets, _members(resolver, fact, bindings)


def qualified(resolver, fact, content, targets, members):
    role = content.get("source_role")
    if fact.field_key.startswith("drug_evidence."):
        if role not in {"CURRENT_RESULT", "REPORT_DRUG_EVIDENCE"}:
            return False
    elif fact.field_key in ASSAY_MEMBERS:
        if role not in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"}:
            return False
    elif role != "CURRENT_RESULT":
        return False
    if fact.field_key == "variant.identity":
        value = content["value"]
        if value["status"] != "COMPLETE" or value["scope"] == "UNKNOWN":
            return False
    if fact.field_key == "assay.negative_statement" and content["value"]["scope"]["state"] != "EXPLICIT":
        return False
    if fact.field_key == "assay.negative_statement":
        from .molecular_source_codes import assertion_codes
        if content["value"]["assertion"] not in assertion_codes(content["value"]["text"]):
            return False
    by_slot = {}
    for member in members:
        if member.field_key in COMPONENT_KEYS - {"variant.tier"}:
            # Repeated transcript/coding/protein/codon/location components are
            # checked against the identity's ordered original lists below.
            # Different printed members of that same list are not conflicts.
            continue
        slot = member.entity_key, member.field_key
        value = resolver._state(member)["content"]["value"]
        if slot in by_slot and by_slot[slot] != value:
            return False
        by_slot[slot] = value
    # Standalone component rows cannot contradict their immutable identity.
    # Every derived metric's membership captures these same heads as well.
    if fact.field_key in COMPONENT_KEYS:
        target = targets.get("VARIANT:0")
        if target and not component_agrees(fact.field_key, content["value"], resolver._state(target)["content"]["value"]):
            return False
    for member in members:
        if member.field_key in COMPONENT_KEYS:
            identity = next((target for target in targets.values()
                             if target.field_key == "variant.identity" and target.entity_key == member.entity_key), None)
            if identity and not component_agrees(member.field_key, resolver._state(member)["content"]["value"], resolver._state(identity)["content"]["value"]):
                return False
    return True


def component_agrees(key, value, identity):
    name = key.removeprefix("variant.")
    if name == "tier":
        return True  # Report tier is not a variant identity component.
    source_key = {"codon": "codons", "transcript": "transcripts", "location": "locations"}.get(name, name)
    component = identity.get(source_key)
    if component is None:
        return value.get("state") in {"NOT_PRINTED", "UNKNOWN"}
    if value.get("state") != component.get("state"):
        return False
    if "values" in component:
        return value.get("raw") in component["values"] if component["state"] == "PRINTED" else value.get("raw") is None
    return value == component
