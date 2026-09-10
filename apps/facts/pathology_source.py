"""Optional immutable roles over a field's own original OCR fragments.

Missing declarations are legacy, not permission to infer labels from nearby
text. These helpers compare Unicode positions in the actual same-field rows;
equal words in another region or an ancestor are not positional evidence.
"""
from django.core.exceptions import ValidationError
import re


VERSION = "PATHOLOGY_LITERAL_SOURCE_V1"
ROLES = ("literal", "value", "label")
INLINE_LABELS = {
    "specimen.description": {"标本类型", "样本类型", "标本名称", "标本描述", "送检材料"},
    "specimen.site": {"取材部位", "送检部位"}, "specimen.procedure": {"取材方式"},
    "assay.method": {"检测方法"}, "assay.antibody": {"抗体克隆号", "抗体克隆", "克隆号"},
    "assay.collection_date": {"采样日期", "采集日期", "取材日期"},
    "assay.received_date": {"收样日期", "接收日期", "接收时间", "样本接收日期", "样本接收时间"},
    "assay.report_date": {"报告日期", "报告时间"},
    "specimen.histology": {"组织学诊断", "病理诊断"}, "specimen.differentiation": {"分化程度"},
    "specimen.margin": {"切缘"}, "specimen.invasion": {"脉管浸润", "浸润"},
    "pathology.reported_stage": {"病理分期"},
}


def validate_shape(declaration):
    keys = {"version", *(role + "_fragment_ordinals" for role in ROLES)}
    if not isinstance(declaration, dict) or set(declaration) != keys or declaration["version"] != VERSION:
        raise ValidationError("字段原值、值窗口与标签须使用明确的来源角色模式。")
    for role in ROLES:
        ordinals = declaration[role + "_fragment_ordinals"]
        if (not isinstance(ordinals, list) or len(ordinals) > 100
                or any(type(i) is not int or i < 0 for i in ordinals)
                or len(set(ordinals)) != len(ordinals) or (role != "label" and not ordinals)):
            raise ValidationError("来源角色须引用本字段不同的实际片段；没有标签时保留空列表。")


def covers_positions(proof, required):
    """Every required Unicode interval, including spaces, has the same block."""
    for target in required:
        if not target.ocr_block_id:
            return False
        ranges = sorted((p.start_offset, p.end_offset) for p in proof
                        if p.ocr_block_id == target.ocr_block_id
                        and p.document_page_id == target.document_page_id)
        cursor = target.start_offset
        for start, end in ranges:
            if start > cursor:
                break
            cursor = max(cursor, end)
            if cursor >= target.end_offset:
                break
        if cursor < target.end_offset:
            return False
    return bool(required)


def source_material(fact, *, fragments=None):
    """Return validated roles, or None for untouched undeclared candidates."""
    content = fact.automatic_content
    if "literal_source" not in content:
        return None
    declaration = content["literal_source"]
    validate_shape(declaration)
    if fact.origin != "AUTOMATIC":
        raise ValidationError("原始 OCR 角色不能替代人工原页转录。")
    pieces = list(fact.source_fragments.all()) if fragments is None else list(fragments)
    by_ordinal = {p.ordinal: p for p in pieces}
    if len(by_ordinal) != len(pieces) or any(p.fact_id != fact.pk for p in pieces):
        raise ValidationError("来源角色必须属于本字段。")
    roles = {}
    for role in ROLES:
        ordinals = declaration[role + "_fragment_ordinals"]
        if not set(ordinals) <= by_ordinal.keys():
            raise ValidationError("来源角色引用的字段片段不存在。")
        roles[role] = [by_ordinal[i] for i in ordinals]
    # Persisted extraction has always placed its own literal first, before any
    # copied binding. Declaring an ancestor after the fact cannot move this root.
    if declaration["literal_fragment_ordinals"] != list(range(len(roles["literal"]))):
        raise ValidationError("原值须保留实际自动提取的首组片段。")
    if "\n".join(p.raw_text for p in roles["literal"]) != content["raw_value"]:
        raise ValidationError("原值与自身不可变片段不一致。")
    declared = {p.ordinal: p for group in roles.values() for p in group}
    for piece in declared.values():
        if piece.source_kind != "OCR":
            raise ValidationError("自动来源角色须有原始 OCR 字符位置。")
        piece.clean()
    # Older inline metadata raw_value includes its explicit label. Only that
    # recognized initial label (and whitespace) is exempt from value coverage.
    # A claimed label window cannot exempt a score number or a trailing value.
    prefix = re.match(r"^([^:：]+)[:：]", content["raw_value"])
    label_end = (prefix.end() if prefix and re.sub(r"\s+", "", prefix.group(1))
                 in INLINE_LABELS.get(fact.field_key, set()) else 0)
    raw_position = 0
    for literal in roles["literal"]:
        for relative, char in enumerate(literal.raw_text):
            if char.isspace():
                continue
            role = "label" if raw_position + relative < label_end else "value"
            offset = literal.start_offset + relative
            if not any(p.ocr_block_id == literal.ocr_block_id and p.document_page_id == literal.document_page_id
                       and p.start_offset <= offset < p.end_offset for p in roles[role]):
                raise ValidationError("值与标签窗口没有覆盖自身原文的实际字符位置。")
        raw_position += len(literal.raw_text) + 1  # exact persisted joining newline
    for value in roles["value"]:
        if not any(value.ocr_block_id == literal.ocr_block_id and value.document_page_id == literal.document_page_id
                   and min(value.end_offset, literal.end_offset) > max(value.start_offset, literal.start_offset)
                   for literal in roles["literal"]):
            raise ValidationError("值窗口不能借用祖先或同文的其他位置。")
    return roles
