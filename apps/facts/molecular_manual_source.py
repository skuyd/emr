"""Immutable first-group page transcriptions; copied anchors remain private."""
from django.core.exceptions import ValidationError

VERSION = "MOLECULAR_MANUAL_SOURCE_V1"


def validate_shape(value):
    if (not isinstance(value, dict) or set(value) != {"version", "own_fragment_count"} or value["version"] != VERSION
            or type(value["own_fragment_count"]) is not int or not 1 <= value["own_fragment_count"] <= 100):
        raise ValidationError("人工分子来源须声明实际首组原文片段数量。")


def own_material(fact, pieces=None):
    if fact.origin != "MANUAL":
        raise ValidationError("人工分子来源不能替代实际 OCR 来源。")
    pieces = list(fact.source_fragments.all()) if pieces is None else list(pieces)
    declaration = fact.automatic_content.get("manual_source")
    if declaration is None:
        # Earlier, not-yet-delivered molecular candidates did not distinguish
        # copied material. Treat their whole source as one conservative window.
        return sorted(pieces, key=lambda p: p.ordinal)
    validate_shape(declaration)
    count = declaration["own_fragment_count"]
    ordered = sorted(pieces, key=lambda p: p.ordinal)
    if count > len(ordered) or [p.ordinal for p in ordered] != list(range(len(ordered))) or any(p.fact_id != fact.pk or p.source_kind != "MANUAL" for p in ordered):
        raise ValidationError("人工原文必须是本字段实际、有序且连续的首组片段。")
    context = fact.automatic_content["entity_context"]
    copied = {i for binding in context["bindings"] if binding["state"] == "BOUND" for i in binding["proof_fragment_ordinals"]}
    association = context["association"]
    if association and association["state"] == "EXPLICIT":
        copied.update(association["proof_fragment_ordinals"])
    if not set(range(count, len(ordered))) <= copied:
        raise ValidationError("不能用首组计数丢弃未归属的原文修饰；首组之外只能保留实际关联依据。")
    return ordered[:count]
