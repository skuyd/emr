"""Literal labeled rows and explicit tables; no clinical interpretation or gold input."""
from dataclasses import dataclass, field
import re

from django.core.exceptions import ValidationError

from .clinical_schema import FIELDS, validate_value
from .clinical_segments import ReportText, _box
from .extraction import explicit_dates
from .molecular_schema import DATE_KEYS
from .pathology_extraction import PathologyCandidate, _views, _line_pieces

EXTRACTOR_VERSION = "molecular-reported-v1"

# These are headings, not dictionaries of genes, drugs or clinical thresholds.
LABELS = {
    "标本编号": "specimen.identity", "样本编号": "specimen.identity", "标本类型": "specimen.description",
    "样本类型": "specimen.description", "取材部位": "specimen.site", "取材方式": "specimen.procedure",
    "检测名称": "assay.identity", "检测项目": "assay.identity", "检测方法": "assay.molecular_method",
    "Panel名称": "assay.panel_name", "panel名称": "assay.panel_name", "Panel规模": "assay.panel_size", "panel规模": "assay.panel_size",
    "采样日期": "assay.collection_date", "收样日期": "assay.received_date", "接收日期": "assay.received_date", "报告日期": "assay.report_date",
    "基因": "gene", "完整表达": "expression", "变异表达": "expression", "编码位点": "coding", "核酸变化": "coding",
    "蛋白位点": "protein", "蛋白变化": "protein", "密码子": "codon", "转录本": "transcript", "位置": "location",
    "拷贝数变化": "change", "融合表达": "fusion", "融合方向": "fusion_order", "变异分级": "tier",
    "5'转录本": "left_transcript", "3'转录本": "right_transcript", "5'断点": "left_breakpoint", "3'断点": "right_breakpoint",
    "变异丰度": "variant.allele_fraction", "等位基因频率": "variant.allele_fraction", "VAF": "variant.allele_fraction",
    "拷贝数": "variant.copy_number", "MSI类别": "assay.msi_category", "MSI数值": "assay.msi_value",
    "TMB": "assay.tmb_value", "TMB数值": "assay.tmb_value", "TMB定性": "assay.tmb_qualitative",
    "药物": "drug_evidence.drugs", "药物依据": "drug_evidence.statement", "依据方向": "drug_evidence.direction",
    "证据等级": "grade", "等级体系": "system", "依据上下文": "drug_evidence.context", "关联变异": "association",
    "检测范围结论": "assay.negative_statement", "检测范围": "negative_scope", "检测种类": "detection_kinds",
    "检测目标": "negative_targets", "检测限制": "negative_limitations",
    "结果断言": "reported_assertion", "MSI": "msi_mixed",
}
LABEL = re.compile("(" + "|".join(re.escape(label) for label in sorted(LABELS, key=len, reverse=True)) + r")[:：]")
NON_CURRENT = re.compile(r"^(历史|既往|上次|送检诊断|临床诊断|说明|备注|文献|解释|质控|质量控制|阳性对照|阴性对照|对照)")
CURRENT = re.compile(r"^(?:体细胞|胚系)?(?:变异)?检测结果(?:[:：]|$)")


@dataclass
class MolecularCandidate(PathologyCandidate):
    association_raw: str | None = None
    association_fragments: list = field(default_factory=list)
    assertion_code: str | None = None
    assertion_raw: str | None = None
    assertion_fragments: list = field(default_factory=list)


@dataclass
class Cell:
    raw: str
    pieces: list
    label_pieces: list = field(default_factory=list)


def _component(raw=None, *, multiple=False, absent="NOT_PRINTED"):
    state = "PRINTED" if raw else absent
    if raw in {"未提供", "未知", "不详", "/", "-", "未检测"}:
        state, raw = "UNKNOWN", None
    elif raw == "未印刷":
        state, raw = "NOT_PRINTED", None
    return {"state": state, "values": [raw] if raw else []} if multiple else {"state": state, "raw": raw}


def _cells(view):
    matches = list(LABEL.finditer(view.text))
    values = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(view.text)
        while end > match.end() and view.text[end - 1] in ";；|":
            end -= 1
        if end <= match.end():
            continue
        key = LABELS[match.group(1)]
        if key in values:
            return {}  # Repeated slots on one ambiguous row are not last-write wins.
        values[key] = Cell(view.raw(match.end(), end), view.fragments(match.end(), end), view.fragments(match.start(), match.end()))
    return values


def _separated(view):
    if "|" not in view.text:
        return None
    cells, start = [], 0
    for match in re.finditer(r"\||$", view.text):
        end = match.start()
        if end > start:
            cells.append(Cell(view.raw(start, end), view.fragments(start, end)))
        else:
            cells.append(Cell("", []))
        start = match.end()
    return cells


def _separate_metadata(segment):
    located = [(p, _box(p.block), ReportText([p]).text.rstrip(":：")) for p in _line_pieces(segment)
               if p.start == 0 and p.end == len(p.block.text) and len(p.text.splitlines()) == 1 and _box(p.block)]
    result = {}
    for label_piece, label_box, label in located:
        if label not in LABELS:
            continue
        row = [(p, box, text) for p, box, text in located if p.page == label_piece.page
               and min(box[3], label_box[3]) - max(box[1], label_box[1]) >= min(box[3] - box[1], label_box[3] - label_box[1]) / 2]
        row.sort(key=lambda item: item[1][0])
        if any(a[1][2] >= b[1][0] for a, b in zip(row, row[1:])):
            continue
        index = next(i for i, item in enumerate(row) if item[0] is label_piece)
        candidates = []
        for piece, _, text in row[index + 1:]:
            if text in LABELS:
                break
            candidates.append(piece)
        if len(candidates) == 1 and candidates[0].text.strip():
            value = candidates[0]
            result[(label_piece.block.pk, label_piece.start)] = (LABELS[label], Cell(value.text.strip(), [value], [label_piece]))
    return result


def _records(segment):
    headers, header_boxes, header_page, header_cells = None, None, None, []
    separate = _separate_metadata(segment)
    for _, view in _views(segment):
        page = view.pieces[0].page
        if page != header_page:
            headers, header_boxes = None, None
        cells = _separated(view)
        if cells is not None:
            header_boxes = None
        if cells is None and len(view.pieces) > 1:
            raw_cells = [Cell(p.text.strip(), [p]) for p in view.pieces]
            if all(c.raw in LABELS for c in raw_cells):
                cells = raw_cells
                header_boxes = [_box(p.block) for p in view.pieces]
        if cells and all(c.raw in LABELS for c in cells) and len({LABELS[c.raw] for c in cells}) == len(cells):
            headers, header_page = [LABELS[c.raw] for c in cells], page
            header_cells = cells
            yield view, {}, True
            continue
        if headers and cells and len(cells) == len(headers):
            for cell in cells:
                cell.label_pieces = [p for header in header_cells for p in header.pieces]
            yield view, {key: cell for key, cell in zip(headers, cells) if cell.raw}, True
            continue
        if headers and header_boxes and all(header_boxes) and len(view.pieces) > 1:
            mapped = {}
            for piece in view.pieces:
                box = _box(piece.block)
                matches = [i for i, head in enumerate(header_boxes) if box and min(box[2], head[2]) > max(box[0], head[0])]
                if len(matches) != 1 or headers[matches[0]] in mapped:
                    mapped = {}
                    break
                mapped[headers[matches[0]]] = Cell(piece.text.strip(), [piece], [p for header in header_cells for p in header.pieces])
            if mapped:
                yield view, mapped, True
                continue
        if cells and "unmapped_molecular_table_row" not in segment.limitations:
            segment.limitations.append("unmapped_molecular_table_row")
        explicit = _cells(view)
        for piece in view.pieces:
            pair = separate.get((piece.block.pk, piece.start))
            if pair and pair[0] not in explicit:
                explicit[pair[0]] = pair[1]
        if explicit or NON_CURRENT.match(view.text) or CURRENT.match(view.text):
            headers, header_boxes = None, None
        yield view, explicit, False


def _quantity(cell, kind):
    normalized = ReportText(cell.pieces).text
    pattern = r"(?P<approx>约|~|≈)?(?P<op><=|>=|≤|≥|<|>)?(?P<first>[0-9]+(?:\.[0-9]+)?)(?:[-–—~至](?P<last>[0-9]+(?:\.[0-9]+)?))?(?P<unit>%|％|mut/Mb|muts/Mb|个|copies)?(?P<object>基因|位点|区域)?"
    match = re.fullmatch(pattern, normalized, re.I)
    value = {"status": "UNRESOLVED", "values": [], "comparator": None, "unit": None, "unit_state": "UNKNOWN",
             "approximate": False, "measurement_kind": kind, "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": cell.raw}
    if kind == "PANEL_SIZE":
        value["count_object"] = _component()
    if match and not (match["last"] and match["op"]):
        value.update(status="PARSED", values=[match["first"]] + ([match["last"]] if match["last"] else []),
                     comparator="RANGE" if match["last"] else {"<=": "LE", "≤": "LE", ">=": "GE", "≥": "GE", "<": "LT", ">": "GT"}.get(match["op"], "EQ"),
                     unit=match["unit"], unit_state="PRINTED" if match["unit"] else "NOT_PRINTED", approximate=bool(match["approx"]))
        if kind == "PANEL_SIZE":
            value["count_object"] = _component(match["object"])
    return value


def _identity(cells, scope):
    raw = lambda key: cells[key].raw if key in cells else None
    absent = "UNKNOWN"  # Missing OCR, including a missing column, cannot prove absence.
    common = {"scope": scope, "status": "COMPLETE" if scope != "UNKNOWN" else "INCOMPLETE"}
    if "fusion" in cells:
        partners = (raw("fusion") or "").split("::")
        if len(partners) != 2 or not all(p.strip() for p in partners):
            return None
        order = "FIVE_TO_THREE" if raw("fusion_order") in {"5'→3'", "5′→3′", "5'至3'"} else "AS_PRINTED_UNKNOWN"
        if order == "AS_PRINTED_UNKNOWN":
            common["status"] = "INCOMPLETE"
        if any(key not in cells for key in ("left_transcript", "right_transcript", "left_breakpoint", "right_breakpoint")):
            common["status"] = "INCOMPLETE"
        return {**common, "kind": "FUSION", "raw": raw("fusion"), "expression": _component(raw("fusion")),
                "order_meaning": order, "partners": [{"gene": _component(gene.strip()),
                    "transcripts": _component(raw(side + "_transcript"), multiple=True, absent=absent),
                    "breakpoints": _component(raw(side + "_breakpoint"), multiple=True, absent=absent)} for side, gene in zip(("left", "right"), partners)]}
    if "gene" not in cells:
        return None
    if "change" in cells:
        return {**common, "kind": "COPY_NUMBER", "raw": raw("gene") + " " + raw("change"),
                "gene": _component(raw("gene")), "change": _component(raw("change"))}
    if "expression" not in cells and "coding" not in cells and "protein" not in cells:
        return None
    expression = raw("expression") or " ".join(cells[k].raw for k in ("coding", "protein") if k in cells)
    if any(key not in cells for key in ("coding", "protein", "codon", "transcript", "location")):
        common["status"] = "INCOMPLETE"
    return {**common, "kind": "SMALL_VARIANT", "raw": raw("gene") + " " + expression,
            "gene": _component(raw("gene")), "expression": _component(expression),
            **{plural: _component(raw(single), multiple=True, absent=absent) for single, plural in
               (("coding", "coding"), ("protein", "protein"), ("codon", "codons"), ("transcript", "transcripts"), ("location", "locations"))}}


def _unknown_component(value):
    if isinstance(value, dict):
        return value.get("state") == "UNKNOWN" or any(_unknown_component(v) for v in value.values())
    return isinstance(value, list) and any(_unknown_component(v) for v in value)


def _ihc_sections(segment):
    from .pathology_extraction import pathology_candidates
    from .molecular_segments import MolecularSegment

    groups, current, metadata, section_heading, excluded = [], None, [], [], set()
    for _, view in _views(segment):
        cells = _cells(view)
        if NON_CURRENT.match(view.text) or CURRENT.fullmatch(view.text):
            section_heading = list(view.pieces)
        if current is None and "specimen.identity" in cells and "assay.identity" not in cells:
            metadata.extend(view.pieces)
        assay = cells.get("assay.identity")
        if assay:
            if current is not None:
                groups.append(current)
                current = None
            if re.search(r"免疫组化|免疫组织化学|\bIHC\b", assay.raw, re.I):
                current = MolecularSegment(segment.title, [*section_heading, *metadata], list(segment.limitations))
        if current is not None:
            current.pieces.extend(view.pieces)
            excluded.update((p.block.pk, p.start, p.end) for p in view.pieces)
    if current is not None:
        groups.append(current)
    result = []
    for number, group in enumerate(groups):
        candidates = pathology_candidates(group)
        mapping = {c.node_id: f"molecular-ihc:{number}:{c.node_id}" for c in candidates}
        for candidate in candidates:
            candidate.node_id = mapping[candidate.node_id]
            kind, identity = candidate.entity.split(":", 1)
            candidate.entity = f"{kind}:m-ihc-{number}-{identity}"
            candidate.links = {role: mapping[node] if node else None for role, node in candidate.links.items()}
            result.append(candidate)
    return result, excluded


def _negative(cells):
    cell = cells.get("assay.negative_statement")
    if cell is None:
        return None
    assertion = next((code for pattern, code in ((r"不确定|可疑|可能", "UNCERTAIN"), (r"未检出|未检测到", "NOT_DETECTED"),
                                                  (r"未检测|未做检测", "NOT_TESTED"), (r"未提供", "NOT_PROVIDED"),
                                                  (r"阴性|未见", "NEGATIVE")) if re.search(pattern, cell.raw)), None)
    if assertion is None:
        return None
    kinds, scope = cells.get("detection_kinds"), cells.get("negative_scope")
    value = {"text": cell.raw, "assertion": assertion, "scope": {
        "state": "UNKNOWN", "raw": None, "detection_kinds": [], "targets": [], "limitations": []}}
    if kinds and scope:
        declared = re.split(r"[、,，;；]", kinds.raw)
        mapping = {"小变异": "SMALL_VARIANT", "SNV/Indel": "SMALL_VARIANT", "拷贝数": "COPY_NUMBER",
                   "融合": "FUSION", "MSI": "MSI", "TMB": "TMB"}
        value["scope"].update(state="EXPLICIT", raw=scope.raw,
                              detection_kinds=[{"code": mapping.get(k.strip(), "OTHER"), "raw": k.strip()} for k in declared if k.strip()])
        value["scope"]["targets"] = [cells["negative_targets"].raw] if "negative_targets" in cells else []
        value["scope"]["limitations"] = [cells["negative_limitations"].raw] if "negative_limitations" in cells else []
    return value


def molecular_candidates(segment):
    result, specimens, assays, variants = [], [], [], []
    ihc_candidates, ihc_pieces = _ihc_sections(segment)
    current_assay, current_specimen, scope, source_role = None, None, "UNKNOWN", "CURRENT_RESULT"

    def statement_supported(code, raw, cells, **kwargs):
        from .molecular_assertion_source import continuous_ranges, statement_windows, supports_original_statement
        windows = []
        for cell in cells:
            for piece in cell.pieces:
                ranges = continuous_ranges(piece.block.text, [(p.start, p.end) for p in segment.pieces if p.block.pk == piece.block.pk])
                for start, end in ranges:
                    if start <= piece.start and end >= piece.end:
                        windows.extend(statement_windows(piece.block.text[start:end], start=piece.start-start, end=piece.end-start))
        return supports_original_statement(code, raw, windows, **kwargs)

    def emit(key, entity, value, cells, links, *, role=None, association=None, association_pieces=(), assertion=None):
        from .molecular_coded_source import TERMS, requirement, table_windows
        if key in TERMS:
            try:
                code, classify = requirement(key, value)
                windows = table_windows(segment, key, value["raw"], [p for cell in cells for p in cell.pieces])
                supported = (all(classify(window) == {code} for window in windows) if windows is not None else
                             statement_supported(code, value["raw"], cells, classify=classify))
            except ValidationError:
                supported = False
            if not supported:
                if "unclassified_molecular_category_source" not in segment.limitations:
                    segment.limitations.append("unclassified_molecular_category_source")
                return None
        assertion_code = {"阳性": "POSITIVE", "检出": "DETECTED", "明确检出": "DETECTED", "阴性": "NEGATIVE", "未检出": "NOT_DETECTED",
                          "不确定": "UNCERTAIN", "未检测": "NOT_TESTED", "未提供": "NOT_PROVIDED"}.get(assertion.raw) if assertion else None
        if assertion_code and not statement_supported(assertion_code, assertion.raw, [assertion]):
            assertion_code = None
        if assertion and not assertion_code and "unclassified_molecular_assertion" not in segment.limitations:
            segment.limitations.append("unclassified_molecular_assertion")
        if assertion_code:
            cells = [*cells, assertion]
        pieces, seen = [], set()
        for cell in cells:
            for piece in cell.pieces:
                position = (piece.block.pk, piece.start, piece.end)
                if position not in seen:
                    pieces.append(piece)
                    seen.add(position)
        if not pieces:
            return None
        if key == "assay.negative_statement" and not statement_supported(value["assertion"], value["text"], cells):
            if "unclassified_molecular_statement" not in segment.limitations:
                segment.limitations.append("unclassified_molecular_statement")
            return None
        try:
            validate_value(key, value)
        except ValidationError:
            segment.limitations.append("unresolved_molecular_value")
            return None
        if value.get("status") in {"UNRESOLVED", "INCOMPLETE"}:
            limitation = "incomplete_molecular_identity" if value["status"] == "INCOMPLETE" else "unresolved_molecular_value"
            if limitation not in segment.limitations:
                segment.limitations.append(limitation)
        actual_role = role or source_role
        existing = next((c for c in result if c.key == key and c.entity == entity and c.value == value and c.links == links
                         and c.source_role == actual_role and c.association_raw == association
                         and c.assertion_raw == (assertion.raw if assertion_code else None)), None)
        if existing:
            def union(original, additional):
                seen = {(p.block.pk, p.start, p.end) for p in original}
                for p in additional:
                    identity = p.block.pk, p.start, p.end
                    if identity not in seen:
                        original.append(p)
                        seen.add(identity)
            union(existing.fragments, pieces)
            union(existing.value_fragments, pieces)
            union(existing.label_fragments, [p for cell in cells for p in cell.label_pieces])
            union(existing.association_fragments, association_pieces)
            union(existing.assertion_fragments, assertion.pieces if assertion_code else [])
            existing.raw_value = "\n".join(p.text for p in existing.fragments)
            return existing
        item = MolecularCandidate(key, entity, value, pieces, "\n".join(p.text for p in pieces),
                                  f"molecular:{len(result)}", links, role or source_role,
                                  value_fragments=list(pieces), label_fragments=[p for cell in cells for p in cell.label_pieces], association_raw=association,
                                  association_fragments=list(association_pieces), assertion_code=assertion_code,
                                  assertion_raw=assertion.raw if assertion_code else None,
                                  assertion_fragments=assertion.pieces if assertion_code else [])
        result.append(item)
        return item

    for view, cells, table in _records(segment):
        if any((p.block.pk, p.start, p.end) in ihc_pieces for p in view.pieces):
            continue
        was_noncurrent = source_role != "CURRENT_RESULT"
        match = NON_CURRENT.match(view.text)
        if match:
            source_role = {"历史": "HISTORICAL_QUOTE", "既往": "HISTORICAL_QUOTE", "上次": "HISTORICAL_QUOTE",
                           "送检诊断": "SUBMITTED_HISTORY", "临床诊断": "SUBMITTED_HISTORY", "质控": "QC", "质量控制": "QC",
                           "阳性对照": "CONTROL", "阴性对照": "CONTROL", "对照": "CONTROL"}.get(match.group(), "EXPLANATION")
        elif CURRENT.match(view.text):
            source_role = "CURRENT_RESULT"
            if was_noncurrent:
                current_assay, current_specimen = None, None
        if source_role == "CURRENT_RESULT" and CURRENT.match(view.text):
            scope = "SOMATIC" if view.text.startswith("体细胞") else "GERMLINE" if view.text.startswith("胚系") else "UNKNOWN"
        metadata_role = "PRIMARY_ASSAY_METADATA" if source_role == "CURRENT_RESULT" else source_role
        if "msi_mixed" in cells:
            cell = cells.pop("msi_mixed")
            cells["assay.msi_value" if _quantity(cell, "MSI")["status"] == "PARSED" else "assay.msi_category"] = cell
        result_slots = {"variant.allele_fraction", "variant.copy_number", "assay.tmb_value", "assay.msi_value", "assay.msi_category", "assay.tmb_qualitative"} & cells.keys()
        assertion = cells.get("reported_assertion") if len(result_slots) == 1 else None
        if len(result_slots) > 1 and "reported_assertion" in cells and "ambiguous_result_assertion" not in segment.limitations:
            segment.limitations.append("ambiguous_result_assertion")
        if "specimen.identity" in cells:
            cell = cells["specimen.identity"]
            item = emit("specimen.identity", f"specimen:m{len(specimens)}", {"label": cell.raw, "raw": cell.raw}, [cell], {}, role=metadata_role)
            if item is None:
                continue
            specimens.append(item)
            current_specimen = item if len(specimens) == 1 or current_assay else None
            current_assay = None
        if "assay.identity" in cells:
            cell = cells["assay.identity"]
            item = emit("assay.identity", f"assay:m{len(assays)}", {"label": cell.raw, "raw": cell.raw}, [cell],
                        {"SPECIMEN": current_specimen.node_id if current_specimen else None}, role=metadata_role)
            if item is None:
                continue
            assays.append(item)
            current_assay = item
            emit("assay.name", item.entity, {"text": cell.raw}, [cell],
                 {"SPECIMEN": current_specimen.node_id if current_specimen else None, "ASSAY": item.node_id}, role=metadata_role)
        parents = {"SPECIMEN": current_specimen.node_id if current_specimen else None, "ASSAY": current_assay.node_id if current_assay else None}
        assay_entity = current_assay.entity if current_assay else "assay:unlinked"
        specimen_entity = current_specimen.entity if current_specimen else "specimen:unlinked"
        for key, cell in cells.items():
            if key in {"specimen.description", "specimen.site", "specimen.procedure"}:
                emit(key, specimen_entity, {"text": cell.raw}, [cell], {"SPECIMEN": parents["SPECIMEN"]}, role=metadata_role)
            elif key in {"assay.panel_name", "assay.molecular_method"}:
                emit(key, assay_entity, {"text": cell.raw}, [cell], parents, role=metadata_role)
            elif key in DATE_KEYS:
                dates = explicit_dates(cell.raw)
                date = dates[0] if len({d["value"] for d in dates}) == 1 else None
                emit(key, assay_entity, {"value": date["value"] if date else None, "precision": date["precision"] if date else "UNKNOWN"},
                     [cell], parents, role=metadata_role)
            elif key in {"assay.panel_size", "assay.tmb_value", "assay.msi_value"}:
                emit(key, assay_entity, _quantity(cell, FIELDS[key].value_type), [cell], parents,
                     role=metadata_role if key == "assay.panel_size" else None, assertion=assertion if key != "assay.panel_size" else None)
            elif key in {"assay.msi_category", "assay.tmb_qualitative"}:
                code = ({"MSI-H": "MSI_H", "MSI-L": "MSI_L", "MSS": "MSS"} if key == "assay.msi_category" else
                        {"高": "HIGH", "中": "INTERMEDIATE", "低": "LOW", "High": "HIGH", "Low": "LOW"}).get(cell.raw, "UNKNOWN")
                emit(key, assay_entity, {"code": code, "raw": cell.raw}, [cell], parents, assertion=assertion)
        negative = _negative(cells)
        if negative:
            emit("assay.negative_statement", assay_entity, negative,
                 [cells[k] for k in ("assay.negative_statement", "negative_scope", "detection_kinds", "negative_targets", "negative_limitations") if k in cells], parents)
        elif "assay.negative_statement" in cells and "unclassified_molecular_statement" not in segment.limitations:
            segment.limitations.append("unclassified_molecular_statement")
        identity_value = _identity(cells, scope)
        identity = None
        if identity_value:
            if _unknown_component(identity_value):
                identity_value["status"] = "INCOMPLETE"
            required = ([identity_value["gene"], identity_value["change"]] if identity_value["kind"] == "COPY_NUMBER" else
                        [identity_value["gene"], identity_value["expression"]] if identity_value["kind"] == "SMALL_VARIANT" else
                        [identity_value["expression"], *[p["gene"] for p in identity_value["partners"]]])
            if any(c["state"] != "PRINTED" for c in required):
                identity_value["status"] = "INCOMPLETE"
            keys = {"gene", "expression", "coding", "protein", "codon", "transcript", "location", "change", "fusion", "fusion_order",
                    "left_transcript", "right_transcript", "left_breakpoint", "right_breakpoint"}
            same_identity = next((c for c in variants if identity_value["status"] == "COMPLETE" and c.value == identity_value
                                  and c.links == parents and c.source_role == source_role), None)
            identity = emit("variant.identity", same_identity.entity if same_identity else f"variant:m{len(variants)}", identity_value,
                            [cell for key, cell in cells.items() if key in keys], parents)
            if identity is None:
                continue
            if identity not in variants:
                variants.append(identity)
            for name in ("gene", "expression", "coding", "protein", "codon", "transcript", "location", "change", "tier"):
                if name in cells:
                    emit("variant." + name, identity.entity, _component(cells[name].raw), [cells[name]], {**parents, "VARIANT": identity.node_id})
        for key in ("variant.allele_fraction", "variant.copy_number"):
            if key in cells:
                emit(key, identity.entity if identity else f"variant:unlinked{len(result)}", _quantity(cells[key], FIELDS[key].value_type),
                     [cells[key]], {**parents, "VARIANT": identity.node_id if identity else None}, assertion=assertion)
        if "drug_evidence.drugs" in cells:
            cell = cells["drug_evidence.drugs"]
            association = cells.get("association")
            matched = []
            if association:
                from .pathology_segments import matching_text
                reference = matching_text(association.raw)
                for candidate in variants:
                    full = matching_text(candidate.value["raw"])
                    expression = full if full in reference else matching_text(candidate.value.get("expression", {}).get("raw") or candidate.value["raw"])
                    position = reference.find(expression)
                    if position >= 0:
                        matched.append((position, candidate, expression))
                matched.sort(key=lambda item: item[0])
                residual, cursor, complete = [], 0, True
                for position, candidate, expression in matched:
                    if position < cursor or reference.count(expression) != 1 or any(candidate.links[k] != parents[k] for k in parents):
                        complete = False
                    residual.append(reference[cursor:position])
                    cursor = position + len(expression)
                residual.append(reference[cursor:])
                if re.sub(r"及|与|和|and|or|[、,，;；+()/]", "", "".join(residual), flags=re.I):
                    complete = False
                if not complete or len({position for position, _, _ in matched}) != len(matched) or len(matched) > 8:
                    matched = []
            targets = [candidate for _, candidate, _ in matched]
            # A relation is emitted only from explicit source conjunctions.
            relation, names = "SINGLE", [cell.raw]
            for separator, meaning in ((" 或 ", "OR"), (" or ", "OR"), (" 与 ", "AND"), (" + ", "AND")):
                if separator in cell.raw:
                    names, relation = cell.raw.split(separator), meaning
                    break
            entity = f"drug_evidence:m{len(result)}"
            links = {**parents, "VARIANT": [c.node_id for c in targets] if targets else None}
            association_pieces = [*association.pieces, *(p for target in targets for p in target.value_fragments)] if targets else []
            drug = emit("drug_evidence.drugs", entity, {"names": names, "relation": relation, "raw": cell.raw}, [cell], links,
                        role="REPORT_DRUG_EVIDENCE" if source_role == "CURRENT_RESULT" else source_role,
                        association=association.raw if targets else None, association_pieces=association_pieces)
            if drug is None:
                continue
            links = {**links, "DRUG_EVIDENCE": drug.node_id}
            for key in ("drug_evidence.statement", "drug_evidence.direction", "drug_evidence.context", "drug_evidence.level"):
                own = cells.get(key) if key != "drug_evidence.level" else cells.get("grade")
                if own is None:
                    continue
                if key == "drug_evidence.direction":
                    code = {"可能获益": "REPORT_BENEFIT", "耐药": "REPORT_RESISTANCE", "未获益": "REPORT_NO_BENEFIT", "不确定": "UNCERTAIN", "未说明": "NOT_STATED"}.get(own.raw)
                    if code is None:
                        if "unclassified_drug_direction" not in segment.limitations:
                            segment.limitations.append("unclassified_drug_direction")
                        continue
                    value = {"code": code, "raw": own.raw}
                elif key == "drug_evidence.level":
                    value = {"grade": _component(own.raw), "system": _component(cells["system"].raw if "system" in cells else None, absent="UNKNOWN"), "raw": own.raw}
                else:
                    value = {"text": own.raw}
                emit(key, entity, value, [own] + ([cells["system"]] if key == "drug_evidence.level" and "system" in cells else []), links,
                     role="REPORT_DRUG_EVIDENCE" if source_role == "CURRENT_RESULT" else source_role,
                     association=association.raw if targets else None, association_pieces=association_pieces)
    result.extend(ihc_candidates)
    if not any(FIELDS[c.key].rank >= 30 for c in result) and "molecular_results_not_parsed" not in segment.limitations:
        segment.limitations.append("molecular_results_not_parsed")
    return result


def persist_molecular_candidates(report, candidates, *, construction_context=None):
    from .molecular_persistence import persist
    return persist(report, candidates, construction_context=construction_context)
