"""Literal pathology candidates with explicit specimen/assay/marker identities.

This module accepts source pieces, not evaluation gold. It does not interpret
clinical significance or repair uncertain OCR characters.
"""
from dataclasses import dataclass, field
import re

from .clinical_segments import Piece, ReportText, _box, logical_lines
from .extraction import explicit_dates


EXTRACTOR_VERSION = "pathology-ihc-v2"
LABEL = re.compile(
    r"(?P<label>标本编号|标本号|样本编号|蜡块编号|组织块号|标本类型|样本类型|送检材料|标本名称|标本描述|取材部位|送检部位|取材方式|"
    r"检测项目|检测名称|检测方法|抗体克隆号|抗体名称|抗体克隆|克隆号|"
    r"采样日期|采集日期|取材日期|收样日期|接收日期|接收时间|报告日期|报告时间|"
    r"组织学诊断|病理诊断|分化程度|切缘|脉管浸润|浸润|病理分期|标本大小|肿瘤大小|淋巴结计数|"
    r"检测结果|染色结果|免疫组化结果|免疫组织化学结果|"
    r"质控|质量控制|阳性对照|阴性对照|说明|备注|临床诊断|送检诊断)[:：]"
)
NON_RESULT = re.compile(r"^(?:质控|质量控制|阳性对照|阴性对照|说明|备注|解释|临床诊断|送检诊断)|(?:既往|历史|上次).{0,12}(?:结果|TPS|CPS)")
NON_CURRENT = re.compile(r"既往|历史|上次|对照|质控|参考|示例|计划|拟检测|待测|未检测|未做检测")
NOT_MEASURED = re.compile(r"待测|未检测|未做检测")
# Recognition names are literal aliases; no cancer-specific ranking or threshold.
MARKER = re.compile(r"PD[-‐‑–]?L1|HER[-‐‑–]?2|Ki[-‐‑–]?67|MLH1|MSH2|MSH6|PMS2|ALK|ER|PR|TTF[-‐‑–]?1|NapsinA|P40|P63", re.I)
SCORE = re.compile(r"(?<![A-Za-z])(?P<kind>TPS|CPS|IC)\s*[:：=]?\s*(?P<approx>约|~|≈)?\s*(?P<comparison><=|>=|≤|≥|<|>)?\s*(?P<values>\d+(?:\.\d+)?(?:\s*[-–—~至]\s*\d+(?:\.\d+)?)?)\s*(?P<unit>%|％|分)?", re.I)
UNAVAILABLE = re.compile(r"^(?:[/／-]|未提供|未注明|不详|未知|未检测|无)$")
DATES = {"采样日期": "collection_date", "采集日期": "collection_date", "取材日期": "collection_date",
         "收样日期": "received_date", "接收日期": "received_date", "接收时间": "received_date",
         "报告日期": "report_date", "报告时间": "report_date"}
TEXT_FIELDS = {"标本类型": "specimen.description", "标本名称": "specimen.description", "标本描述": "specimen.description",
               "样本类型": "specimen.description", "送检材料": "specimen.description",
               "取材部位": "specimen.site", "送检部位": "specimen.site", "取材方式": "specimen.procedure",
               "抗体克隆号": "assay.antibody", "抗体克隆": "assay.antibody", "克隆号": "assay.antibody"}
ASSERTED_FIELDS = {"组织学诊断": "specimen.histology", "病理诊断": "specimen.histology",
                   "分化程度": "specimen.differentiation", "切缘": "specimen.margin", "脉管浸润": "specimen.invasion",
                   "浸润": "specimen.invasion", "病理分期": "pathology.reported_stage"}


@dataclass
class PathologyCandidate:
    key: str
    entity: str
    value: dict
    fragments: list
    raw_value: str
    node_id: str
    links: dict = field(default_factory=dict)
    source_role: str = "CURRENT_RESULT"
    limitations: tuple = ()
    transformations: tuple = ()


def _line_pieces(segment):
    # Split raw Unicode lines without ever treating concatenated text offsets
    # as offsets in an OCR block.
    for piece in segment.pieces:
        offset = piece.start
        for line in piece.text.splitlines(keepends=True):
            yield Piece(piece.block, offset, offset + len(line))
            offset += len(line)


def _views(segment):
    pieces = list(_line_pieces(segment))
    # A selected subrange of a multi-line block must not pull in the rest of
    # that block (possibly a second report). Whole one-line boxes may be joined
    # using layout geometry, while offsets/polygons remain original.
    if all(piece.start == 0 and piece.end == len(piece.block.text) for piece in pieces):
        return [(group[0], ReportText(group)) for _, group in logical_lines([piece.block for piece in pieces])]
    return [(piece, ReportText([piece])) for piece in pieces]


def _score_value(view, match):
    from .clinical_schema import validate_value
    from django.core.exceptions import ValidationError

    if match is None:
        return None
    suffix = view.text[match.end():]
    if suffix and not re.match(r"(?:TPS|CPS|IC)[:：=]", suffix, re.I) and re.match(r"[A-Za-z0-9.+\-/%]", suffix):
        return None
    numbers = re.split(r"[-–—~至]", match.group("values"))
    if len(numbers) == 2 and match.group("comparison"):
        return None  # Do not discard a conflicting second qualifier.
    kind = match.group("kind").upper()
    unit = match.group("unit")
    unit = "%" if unit == "％" else unit
    value = {"score_kind": kind, "values": numbers,
             "comparator": "RANGE" if len(numbers) == 2 else {"<=": "LE", "≤": "LE", ">=": "GE", "≥": "GE", "<": "LT", ">": "GT"}.get(match.group("comparison"), "EQ"),
             "unit": unit, "unit_state": "PRINTED" if unit else "NOT_PRINTED",
             "scale_kind": "SCORE" if kind == "CPS" else "PROPORTION", "approximate": bool(match.group("approx")),
             "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": view.raw(*match.span())}
    try:
        validate_value("ihc.score", value)
    except ValidationError:
        return None
    return value


def _marker_boundaries(view, match):
    # NFKC's matching view drops whitespace. Check actual neighbouring raw
    # characters so a separate specimen ID does not become part of the marker.
    first_piece, start = view.offsets[match.start()]
    last_piece, end = view.offsets[match.end() - 1]
    before = view.pieces[first_piece].block.text[start - 1:start] if start > view.pieces[first_piece].start else ""
    after = view.pieces[last_piece].block.text[end + 1:end + 2] if end + 1 < view.pieces[last_piece].end else ""
    if before and re.match(r"[A-Za-z0-9-]", before):
        return False
    if after and re.match(r"[A-Za-z0-9-]", after):
        return bool(re.match(r"(?:TPS|CPS|IC)[:：=]", view.text[match.end():], re.I))
    return True


def _tables(segment):
    """Only explicit column headings justify associating separate OCR cells."""
    headings = {"抗体名称": "MARKER", "检测项目": "MARKER", "标记物": "MARKER",
                "克隆号": "CLONE", "抗体克隆号": "CLONE", "检测方法": "METHOD", "检测结果": "RESULT"}
    pieces = list(_line_pieces(segment))
    located = [(piece, _box(piece.block), ReportText([piece])) for piece in pieces
               if piece.start == 0 and piece.end == len(piece.block.text) and _box(piece.block)]
    tables, used = [], set()
    for result_header, result_box, result_view in located:
        if headings.get(result_view.text) != "RESULT":
            continue
        headers = [(headings[view.text], piece, box) for piece, box, view in located
                   if piece.page == result_header.page and view.text in headings
                   and min(box[3], result_box[3]) > max(box[1], result_box[1])]
        roles = [role for role, _, _ in headers]
        if len(roles) != len(set(roles)) or not {"MARKER", "METHOD", "RESULT"} <= set(roles):
            continue
        headers.sort(key=lambda item: item[2][0])
        cuts = [(a[2][2] + b[2][0]) / 2 for a, b in zip(headers, headers[1:])]
        if any(a[2][2] >= b[2][0] for a, b in zip(headers, headers[1:])):
            continue
        top = max(box[3] for _, _, box in headers)
        below = [(piece, box, view) for piece, box, view in located if piece.page == result_header.page and box[1] >= top]
        stop = min((box[1] for _, box, view in below if re.match(r"质控|质量|对照|镜下|说明|备注|报告(?:日期|时间)|检测结果说明|本报告", view.text)), default=1.01)
        columns = {role: [] for role in roles}
        for piece, box, view in below:
            if box[1] >= stop:
                continue
            used.add((piece.block.pk, piece.start, piece.end))
            if any(box[0] < cut < box[2] for cut in cuts):
                continue
            position = sum((box[0] + box[2]) / 2 > cut for cut in cuts)
            columns[headers[position][0]].append((piece, box, view))
        # Unknown marker names still occupy real rows. Only recognizing one
        # name does not turn a multi-marker table into a single-marker panel.
        marker_rows = [item for item in columns["MARKER"] if item[2].text]
        markers = [item for item in marker_rows if MARKER.fullmatch(item[2].text)]
        for marker_piece, marker_box, marker_view in markers:
            values = []
            for piece, box, view in columns["RESULT"]:
                if NON_CURRENT.search(view.text):
                    continue
                aligned = min(box[3], marker_box[3]) > max(box[1], marker_box[1])
                if len(marker_rows) != 1 and not aligned:
                    continue
                if any(other_piece.block.pk != marker_piece.block.pk and min(box[3], other_box[3]) > max(box[1], other_box[1])
                       for other_piece, other_box, _ in marker_rows):
                    continue
                values.append(view)
            attributes = {}
            for role in ("METHOD", "CLONE"):
                aligned = [view for _, box, view in columns.get(role, []) if min(box[3], marker_box[3]) > max(box[1], marker_box[1])]
                if len(aligned) == 1:
                    attributes[role] = aligned[0]
            if values:
                tables.append((marker_view, values, attributes))
    return tables, used


def _assertion(text):
    # This is only a matching view; the candidate retains the complete raw
    # Unicode text. The longer completed-test predicate is not an untested one.
    text = re.sub(r"\s+", "", text)
    if re.search(r"未检测(?!到)|未做|未行检测", text):
        return "NOT_TESTED"
    if re.search(r"不确定|不能确定|可疑|疑似|倾向|不能排除|不除外", text):
        return "UNCERTAIN"
    if re.search(r"未检出|未检测到", text):
        return "NOT_DETECTED"
    if re.search(r"阴性|未见|未发现|无浸润", text):
        return "NEGATIVE"
    if re.search(r"阳性", text):
        return "POSITIVE"
    if re.search(r"检出|检测到", text):
        return "DETECTED"
    return "SOURCE_TEXT_ONLY_NOT_DIAGNOSED"


def _marker_code(raw):
    clean = re.sub(r"[-‐‑–\s]", "", raw).upper()
    return {"PDL1": "PD_L1", "HER2": "HER2", "KI67": "KI67"}.get(clean, clean)


def _node_counts(view, start, end):
    groups, unparsed = [], False
    for match in re.finditer(r"[^;；]+", view.text[start:end]):
        clause = match.group()
        sampled = re.search(r"(?:送检|检出|检查)(\d+)枚", clause)
        positive = re.search(r"(?:转移|阳性)(\d+)枚", clause)
        counts = [item for item in (sampled, positive) if item]
        label_end = min((item.start() for item in counts), default=0)
        label = view.raw(start + match.start(), start + match.start() + label_end).strip(" ,，:：")
        if not counts or not label or re.search(r"\d+[/／]\d+", clause):
            unparsed = True
            continue
        raw = view.raw(start + match.start(), start + match.end())
        groups.append({"label": label, "sampled": sampled.group(1) if sampled else None,
                       "positive": positive.group(1) if positive else None, "raw": raw})
    return groups, unparsed


def pathology_candidates(segment):
    lines = [(piece, view) for piece, view in _views(segment) if view.text]
    output, specimens, assays = [], [], []

    def add(key, entity, value, view, start, end, *, links=None, role="CURRENT_RESULT", limits=()):
        fragments = view.fragments(start, end)
        candidate = PathologyCandidate(key, entity, value, fragments, view.raw(start, end),
                                      f"field:{len(output):04}", links or {}, role, tuple(limits))
        output.append(candidate)
        return candidate

    cells = []
    for line_index, (piece, view) in enumerate(lines):
        labels = list(LABEL.finditer(view.text))
        for i, match in enumerate(labels):
            end = labels[i + 1].start() if i + 1 < len(labels) else len(view.text)
            raw = view.raw(match.end(), end).strip()
            if raw and not UNAVAILABLE.fullmatch(raw):
                cells.append((line_index, match.group("label"), raw, view, match.start(), match.end(), end))

    for index, label, raw, view, start, value_start, end in cells:
        if label in {"标本编号", "标本号", "样本编号", "蜡块编号", "组织块号"}:
            # Repeated literal identifiers on the same report are one anchor;
            # distinct identifiers remain distinct instead of first/last wins.
            existing = next((candidate for _, candidate in specimens if candidate.value["raw"] == raw), None)
            if existing is None:
                candidate = add("specimen.identity", f"specimen:{len(specimens) + 1:03}", {"label": raw, "raw": raw}, view, value_start, end, role="PRIMARY_ASSAY_METADATA")
                specimens.append((index, candidate))
    if not specimens:
        materials = [cell for cell in cells if cell[1] in {"标本类型", "样本类型", "送检材料", "标本名称"}]
        if len(materials) == 1:
            index, _, raw, view, _, value_start, end = materials[0]
            candidate = add("specimen.identity", "specimen:001", {"label": raw, "raw": raw}, view, value_start, end,
                            role="PRIMARY_ASSAY_METADATA", limits=("report_local_material_identity",))
            specimens.append((index, candidate))

    def specimen_for(index, text=""):
        explicit = [candidate for _, candidate in specimens if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(candidate.value["raw"]) + r"(?![A-Za-z0-9_-])", text)]
        if len(explicit) == 1:
            return explicit[0]
        return specimens[0][1] if len(specimens) == 1 else None

    for index, label, raw, view, start, value_start, end in cells:
        if label in {"检测项目", "检测名称"}:
            specimen = specimen_for(index, view.raw(0, len(view.text)))
            candidate = add("assay.identity", f"assay:{len(assays) + 1:03}", {"label": raw, "raw": raw}, view, value_start, end,
                            links={"SPECIMEN": specimen.node_id if specimen else None}, role="PRIMARY_ASSAY_METADATA")
            assays.append((index, candidate))
    if not assays:
        from .pathology_segments import NAMED_ASSAY_TITLE

        for index, (_, view) in enumerate(lines):
            if NAMED_ASSAY_TITLE.fullmatch(view.text):
                specimen = specimen_for(index)
                raw = view.raw(0, len(view.text))
                candidate = add("assay.identity", f"assay:{len(assays) + 1:03}", {"label": raw, "raw": raw}, view, 0, len(view.text),
                                links={"SPECIMEN": specimen.node_id if specimen else None}, role="PRIMARY_ASSAY_METADATA")
                assays.append((index, candidate))

    def assay_for(index, specimen, text=""):
        compatible = [candidate for _, candidate in assays if candidate.links["SPECIMEN"] == (specimen.node_id if specimen else None)]
        explicit = [candidate for candidate in compatible if candidate.value["raw"] in text]
        if len(explicit) == 1:
            return explicit[0]
        return compatible[0] if len(compatible) == 1 else None

    for index, label, raw, view, start, value_start, end in cells:
        if NON_RESULT.match(view.text):
            continue
        specimen = specimen_for(index, view.raw(0, len(view.text)))
        assay = assay_for(index, specimen, view.text)
        key = TEXT_FIELDS.get(label) or ASSERTED_FIELDS.get(label)
        value = {"text": raw}
        if label in ASSERTED_FIELDS:
            value["assertion"] = _assertion(raw)
        if label in DATES:
            key = "assay." + DATES[label]
            dates = explicit_dates(raw)
            if len(dates) != 1:
                continue
            value = {"value": dates[0]["value"], "precision": dates[0]["precision"]}
        elif label == "检测方法":
            key = "assay.method"
            code = "IHC" if re.fullmatch(r"IHC|免疫组化|免疫组织化学", raw, re.I) else "ISH" if re.fullmatch(r"ISH|原位杂交", raw, re.I) else "OTHER"
            value = {"code": code, "raw": raw}
        elif label in {"标本大小", "肿瘤大小"}:
            from .clinical_extraction import DIMENSION, _dimension_value

            if specimen is None:
                continue
            for measurement in DIMENSION.finditer(view.text[value_start:end]):
                left, right = value_start + measurement.start(), value_start + measurement.end()
                value = _dimension_value(measurement.group(), view.text[value_start:left])
                value.update(measurement_object="SPECIMEN" if label == "标本大小" else "TUMOR",
                             raw=view.raw(left, right))
                add("specimen.dimensions", specimen.entity, value, view, start, right,
                    links={"SPECIMEN": specimen.node_id}, limits=("axes_not_labeled",) if all(c["axis"] is None for c in value["components"]) else ())
            continue
        elif label == "淋巴结计数":
            if specimen is None:
                continue
            groups, unparsed = _node_counts(view, value_start, end)
            if groups:
                add("specimen.nodes", specimen.entity, {"groups": groups, "assertion": _assertion(raw), "raw": raw}, view, start, end,
                    links={"SPECIMEN": specimen.node_id}, limits=("unparsed_node_count_expression",) if unparsed else ())
            continue
        if key:
            if key.startswith("assay."):
                # Without a unique assay anchor, preserve the OCR but do not
                # create unattached assay properties masquerading as an assay.
                if assay is None:
                    continue
                entity, links = assay.entity, {"SPECIMEN": specimen.node_id if specimen else None, "ASSAY": assay.node_id}
            elif key.startswith("specimen."):
                if specimen is None:
                    continue
                entity, links = specimen.entity, {"SPECIMEN": specimen.node_id}
            else:
                entity, links = "report", {}
            add(key, entity, value, view, start, end, links=links, role="PRIMARY_ASSAY_METADATA" if label not in ASSERTED_FIELDS else "CURRENT_RESULT")

    marker_count = 0
    tables, table_pieces = _tables(segment)
    for index, (piece, view) in enumerate(lines):
        text = view.text
        if NON_RESULT.match(text):
            continue
        if any((piece.block.pk, piece.start, piece.end) in table_pieces for piece in view.pieces):
            continue
        # A literal result label or a marker + typed score/result is required.
        # Metadata labels are never interpreted as actual result assertions.
        if any(label in text for label in ("检测项目:", "检测名称:", "抗体克隆号:", "临床诊断:")):
            continue
        matches = [match for match in MARKER.finditer(text) if _marker_boundaries(view, match)]
        for marker_index, match in enumerate(matches):
            if (re.search(r"非$", text[:match.start()])
                    or NON_CURRENT.search(NOT_MEASURED.sub("", text))):
                continue
            end = matches[marker_index + 1].start() if marker_index + 1 < len(matches) else len(text)
            tail = text[match.end():end]
            scores = [] if NOT_MEASURED.search(tail) else list(SCORE.finditer(tail))
            result = re.fullmatch(r"[:：]?(.+)", tail)
            qualitative = result.group(1) if result else ""
            if not scores and _assertion(qualitative) == "SOURCE_TEXT_ONLY_NOT_DIAGNOSED":
                continue
            specimen = specimen_for(index, view.raw(0, len(view.text)))
            assay = assay_for(index, specimen, text)
            links = {"SPECIMEN": specimen.node_id if specimen else None, "ASSAY": assay.node_id if assay else None}
            limits = tuple("unlinked_" + role.lower() for role, target in links.items() if target is None)
            marker_count += 1
            entity = f"ihc:{marker_count:03}"
            marker_raw = view.raw(*match.span())
            marker = add("ihc.marker", entity, {"code": _marker_code(match.group()), "label": marker_raw, "raw": marker_raw}, view,
                         *match.span(), links=links, limits=limits)
            result_links = {**links, "MARKER": marker.node_id}
            for score in scores:
                kind = score.group("kind").upper()
                left, right = match.end() + score.start(), match.end() + score.end()
                value = _score_value(view, SCORE.match(view.text, left))
                if value is None:
                    continue
                add("ihc.score", entity, value, view, left, right, links=result_links, limits=limits)
            if not scores:
                left = match.end() + (1 if tail.startswith((":", "：")) else 0)
                raw_result = view.raw(left, end)
                add("ihc.result", entity, {"text": raw_result, "assertion": _assertion(raw_result)}, view,
                    left, end, links=result_links, limits=limits)
    for marker_view, score_views, attributes in tables:
        specimen = specimen_for(0)
        assay = assay_for(0, specimen)
        links = {"SPECIMEN": specimen.node_id if specimen else None, "ASSAY": assay.node_id if assay else None}
        limits = tuple("unlinked_" + role.lower() for role, target in links.items() if target is None)
        marker_count += 1
        entity = f"ihc:{marker_count:03}"
        raw = marker_view.raw(0, len(marker_view.text))
        marker = add("ihc.marker", entity, {"code": _marker_code(marker_view.text), "label": raw, "raw": raw}, marker_view,
                     0, len(marker_view.text), links=links, limits=limits)
        if assay:
            for role, attribute in attributes.items():
                raw_attribute = attribute.raw(0, len(attribute.text))
                key = "assay.method" if role == "METHOD" else "assay.antibody"
                if key == "assay.method":
                    value = {"code": "IHC" if re.fullmatch(r"IHC|免疫组化|免疫组织化学", attribute.text, re.I) else "ISH" if attribute.text == "ISH" else "OTHER", "raw": raw_attribute}
                else:
                    value = {"text": raw_attribute}
                if not any(candidate.key == key and candidate.entity == assay.entity and candidate.value == value for candidate in output):
                    add(key, assay.entity, value, attribute, 0, len(attribute.text), links=links, role="PRIMARY_ASSAY_METADATA")
        for view in score_views:
            for score in SCORE.finditer(view.text):
                value = _score_value(view, score)
                if value is not None:
                    add("ihc.score", entity, value, view, *score.span(), links={**links, "MARKER": marker.node_id}, limits=limits)
    return output


def persist_pathology_candidates(report, candidates, *, construction_context=None):
    """Persist literal candidates and their complete immutable anchor proofs.

    Call under the existing document aggregate transaction. The nested atomic
    block also makes this reusable entry fail as a whole on invalid bindings.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from apps.processing.models import SourceEvidence
    from .clinical_context import validate_context_candidate
    from .clinical_schema import FIELDS, field_content
    from .models import Fact, FactSourceFragment
    from .pathology_schema import CONTEXT

    with transaction.atomic():
        if report.routing_kind != "PATHOLOGY":
            raise ValidationError("病理候选必须属于病理报告。")
        nodes = {candidate.node_id: candidate for candidate in candidates}
        if len(nodes) != len(candidates):
            raise ValidationError("候选的临时实体身份重复。")
        persisted = {}
        for order, candidate in enumerate(sorted(candidates, key=lambda item: FIELDS[item.key].rank)):
            pieces, ordinals = [], {}

            def retain(piece):
                identity = (piece.block.pk, piece.start, piece.end)
                if identity not in ordinals:
                    ordinals[identity] = len(pieces)
                    pieces.append(piece)
                return ordinals[identity]

            for piece in candidate.fragments:
                retain(piece)
            bindings = []
            if set(candidate.links) != set(FIELDS[candidate.key].roles):
                raise ValidationError("候选上下文角色不完整。")
            for role in FIELDS[candidate.key].roles:
                node_id = candidate.links[role]
                target = persisted.get(node_id) if node_id else None
                if node_id is not None and target is None:
                    raise ValidationError("候选锚不存在或不是更低层实体。")
                proof = [retain(piece) for piece in nodes[node_id].fragments] if target else []
                bindings.append({"role": role, "state": "BOUND" if target else "UNKNOWN",
                                 "target_fact_id": str(target.pk) if target else None,
                                 "target_entity_key": target.entity_key if target else None,
                                 "proof_fragment_ordinals": proof,
                                 "reason": None if target else "AMBIGUOUS" if "unlinked_specimen" in candidate.limitations else "NOT_STATED"})
            if not pieces:
                raise ValidationError("自动字段不能没有原文片段。")
            raw_text = "\n".join(piece.text for piece in pieces)
            first = pieces[0]
            evidence = SourceEvidence.objects.create(
                parsing_version=report.parsing_version, document_page=first.block.document_page,
                ocr_block=first.block if len(pieces) == 1 else None,
                polygon=first.block.polygon if len(pieces) == 1 else None,
                source_text=raw_text, confidence=min(piece.block.confidence for piece in pieces),
            )
            context = {"context_version": CONTEXT, "report_id": str(report.pk), "membership_policy": CONTEXT, "bindings": bindings}
            content = field_content(candidate.key, candidate.value, candidate.raw_value, entity_context=context,
                                    source_role=candidate.source_role, limitations=[*report.limitations, *candidate.limitations],
                                    transformations=candidate.transformations)
            fact = Fact(document=report.document, document_page=first.block.document_page, parsing_version=report.parsing_version,
                        evidence=evidence, origin="AUTOMATIC", category="PATHOLOGY", representation="FIELD", clinical_report=report,
                        field_key=candidate.key, entity_key=candidate.entity, schema_version=content["schema_version"],
                        raw_text=raw_text, automatic_content=content, reading_order=order, created_by=report.created_by)
            fact.full_clean()
            fact.save()
            for ordinal, piece in enumerate(pieces):
                fragment_evidence = evidence if len(pieces) == 1 else SourceEvidence.objects.create(
                    parsing_version=report.parsing_version, document_page=piece.block.document_page,
                    ocr_block=piece.block, polygon=piece.block.polygon, source_text=piece.text, confidence=piece.block.confidence,
                )
                fragment = FactSourceFragment(fact=fact, ordinal=ordinal, document_page=piece.block.document_page,
                                              evidence=fragment_evidence, ocr_block=piece.block, source_kind="OCR",
                                              start_offset=piece.start, end_offset=piece.end, raw_text=piece.text,
                                              polygon=piece.block.polygon)
                fragment.full_clean()
                fragment.save()
            validate_context_candidate(fact, construction_context=construction_context)
            persisted[candidate.node_id] = fact
        return len(persisted)
