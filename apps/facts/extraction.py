"""Literal section extraction. It does not infer diagnoses, stages or treatment cycles."""

from collections import defaultdict
from datetime import date
import re

from django.db import transaction

from apps.processing.models import OcrBlock, SourceEvidence
from apps.processing.value_objects import InvalidRegion, normalized_polygon

from .models import Fact, FactCategory, FactExtraction
from .layout import source_lines
from .metadata import page_record_dates


EXTRACTOR_VERSION = "literal-sections-4"
_HEADINGS = {
    "DIAGNOSIS": ("出院诊断", "入院诊断", "临床诊断", "主要诊断", "初步诊断", "诊断"),
    "STAGE": ("临床分期", "病理分期", "TNM分期", "TNM 分期", "肿瘤分期", "分期"),
    "TREATMENT": ("治疗经过", "诊疗经过", "治疗方案", "化疗方案", "药物治疗", "放疗记录", "放疗方案", "手术名称", "手术记录", "现病史"),
    "IMAGING": ("影像学诊断", "影像诊断", "影像结论", "影像检查结论", "诊断提示", "超声提示"),
    "PATHOLOGY": ("病理诊断", "病理结论", "病理检查结论"),
}
_GENERIC_CONCLUSIONS = ("诊断意见", "诊断结论", "检查结论", "检查印象", "结论", "印象")
_STOP_NAMES = (
    "检查所见", "影像所见", "影像表现", "肉眼所见", "镜下所见", "出院医嘱", "医嘱", "报告日期", "报告时间",
    "检查日期", "检查项目", "入院日期", "出院日期", "手术日期", "报告医师", "审核医师", "医师签名", "签名",
    "送检材料", "标本类型", "标本状态", "样本类型", "样本状态", "样本性状", "临床资料", "既往史",
    "入院时情况", "诊断依据", "检验结果", "条码号", "病案号", "患者编号", "报告编号", "床号", "姓名", "年龄",
    "送检单位", "检测仪器", "检测方法", "检测时间", "用药史", "肿瘤样本采集日期", "对照样本采集日期",
    "接收时间", "采样时间", "采集时间", "实验室声明", "项目名称", "序代号", "序号代号", "参考范围",
    "审核时间", "审核医生", "报告医生", "记录员", "来源",
    "样本接收日期", "样本质控结果", "处方医师", "医疗机构",
    "本次检测概览", "检测概览", "送检目的", "检测项目", "检测结果", "判读标准",
)
_STOP = re.compile(r"^(?:" + "|".join(_STOP_NAMES) + r")(?:\s*[:：（(]|\s*$|正常)")
_TABLE_STOP = re.compile(r"^(?:序|序号|代号|结果|单位|提示|床|号[:：].*|测试方法|互认标识|\d{9,})\s*$")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2})\s*日?)?\s*月?|年)(?(3)(?=\D|$|\d{2}[:：]\d{2})|(?!\d|[./-]\s*\d))")
_INLINE_METADATA = re.compile(
    r"(?:接收时间|采样时间|采集时间|检验时间)\s*[:：]"
)
_ORDER_HEADERS = {
    "开始时间", "医嘱类型", "医嘱类别", "医嘱期效", "医嘱内容", "执行科室",
    "执行状态", "开嘱医生", "开立医生", "开单医生", "签名医生",
    "停止时间", "核对医嘱", "停止医嘱", "时间", "医生签名", "护士签名",
}


def _layout_box(block):
    points = normalized_polygon(getattr(block, "layout_polygon", None) or block.polygon)
    x, y = zip(*points)
    return {
        "left": min(x), "right": max(x), "top": min(y), "bottom": max(y),
        "middle": (min(y) + max(y)) / 2, "block": block,
        "text": block.text.strip(),
    }


def _source_fragment(box):
    block = box["block"]
    try:
        polygon = normalized_polygon(block.polygon)
    except (InvalidRegion, TypeError):
        polygon = None
    return {"text": block.text, "polygon": polygon, "reading_order": block.reading_order}


def _join_order_content(boxes):
    parts = [boxes[0]["text"]]
    transforms = []
    for previous, current in zip(boxes, boxes[1:]):
        left_text, right_text = previous["text"], current["text"]
        overlap = previous["right"] - current["left"]
        duplicated = 0
        if overlap > 0 and left_text and right_text:
            character_width = min(
                (previous["right"] - previous["left"]) / len(left_text),
                (current["right"] - current["left"]) / len(right_text),
            )
            # Some OCR regions repeat the character in their shared strip.
            # Matching text alone cannot erase an adjacent repeated dose.
            duplicated = max((size for size in range(1, min(len(left_text), len(right_text)) + 1)
                              if left_text.endswith(right_text[:size])
                              and .75 * character_width * size <= overlap), default=0)
        parts.append(right_text[duplicated:])
        if duplicated:
            transforms.append({
                "rule_version": "medication-overlap-1", "field": "medication_order_content",
                "reason": "相邻 OCR 区域在同一位置重复识别了相同文字，已合并重叠部分；请核对原件中的剂量。",
                "source_fragments": [_source_fragment(previous), _source_fragment(current)],
                "overlapping_text": right_text[:duplicated],
                "before": left_text + " " + right_text,
                "after": left_text + " " + right_text[duplicated:],
            })
    return " ".join(part for part in parts if part), transforms


def _medication_rows(blocks):
    """Transcribe bounded medication table rows; headings alone never establish an order."""
    pages = defaultdict(list)
    for block in blocks:
        try:
            pages[block.document_page_id].append(_layout_box(block))
        except (InvalidRegion, TypeError):
            # Without table geometry there is no safe cross-column association.
            continue
    output = []
    for page, boxes in pages.items():
        headings = []
        for box in boxes:
            name = re.sub(r"\s+", "", box["text"]).strip(":：▲▼△▽")
            if name in _ORDER_HEADERS:
                headings.append({**box, "name": name})
        content_headers = sorted(
            (box for box in headings if box["name"] == "医嘱内容"), key=lambda box: box["middle"],
        )
        for index, content_header in enumerate(content_headers):
            height = content_header["bottom"] - content_header["top"]
            nearby = [box for box in headings if abs(box["middle"] - content_header["middle"]) <= height]
            by_name = {}
            for box in nearby:
                if box["name"] not in by_name or abs(box["middle"] - content_header["middle"]) < abs(
                    by_name[box["name"]]["middle"] - content_header["middle"]
                ):
                    by_name[box["name"]] = box
            duration_name = "医嘱期效" if "医嘱期效" in by_name else "医嘱类型"
            kind_name = "医嘱类型" if duration_name == "医嘱期效" else "医嘱类别"
            if not {"开始时间", kind_name, duration_name, "执行状态"} <= by_name.keys():
                continue
            start, kind, duration, status = (by_name[name] for name in (
                "开始时间", kind_name, duration_name, "执行状态",
            ))
            if not start["left"] < kind["left"] < duration["left"] < content_header["left"] < status["left"]:
                continue
            content_end = min(
                box["left"] for box in nearby if box["left"] > content_header["left"] and box["name"] not in {
                    "时间", "停止医嘱", "医生签名", "护士签名",
                }
            )
            right_neighbors = [box["left"] for box in nearby
                               if box["left"] > status["right"] and box["name"] not in {"停止医嘱"}]
            status_end = (status["right"] + min(right_neighbors)) / 2 if right_neighbors else status["right"] + height
            kind_start = (start["right"] + kind["left"]) / 2
            duration_start = (kind["right"] + duration["left"]) / 2
            content_start = (duration["right"] + content_header["left"]) / 2
            # The second tier of time/signature headers belongs to the header,
            # even if a tall first-row OCR box overlaps its vertical band.
            lower = max(box["bottom"] for box in nearby)
            upper = content_headers[index + 1]["top"] if index + 1 < len(content_headers) else 1.0

            def in_column(box, left, right):
                return left <= (box["left"] + box["right"]) / 2 < right

            body_boxes = [box for box in boxes if lower < box["middle"] < upper]
            # Each order-kind cell fixes a row, including non-medication and clipped rows,
            # so their contents cannot be accidentally absorbed by a neighboring order.
            anchors = sorted(
                (box for box in body_boxes if in_column(box, kind_start, duration_start)),
                key=lambda box: box["middle"],
            )
            stop_header = by_name.get("停止时间") or by_name.get("停止医嘱")
            stop_column = None
            if stop_header:
                time_headers = [box for box in headings if box["name"] in {"时间", "停止时间"}
                                and abs(box["middle"] - content_header["middle"]) <= height * 1.5
                                and box["left"] > content_header["right"]]
                if time_headers:
                    stop_center = (stop_header["left"] + stop_header["right"]) / 2
                    previous_groups = [(box["left"] + box["right"]) / 2 for box in nearby
                                       if box["name"] == "核对医嘱" and box["right"] < stop_header["left"]]
                    group_left = (max(previous_groups) + stop_center) / 2 if previous_groups else status["right"]
                    stop_times = [box for box in time_headers
                                  if (box["left"] + box["right"]) / 2 > group_left]
                    if not stop_times:
                        continue
                    # The first time under the stop group records the stop order;
                    # a later time may be the nurse's acknowledgement of that order.
                    stop_time = stop_header if stop_header["name"] == "停止时间" else min(
                        stop_times, key=lambda box: box["left"],
                    )
                    center = (stop_time["left"] + stop_time["right"]) / 2
                    peers = [(box["left"] + box["right"]) / 2 for box in headings
                             if box["name"] in {"时间", "停止时间", "医生签名", "护士签名"}
                             and abs(box["middle"] - stop_time["middle"]) <= height]
                    left = max((value for value in peers if value < center), default=center - height * 4)
                    right = min((value for value in peers if value > center), default=center + height * 4)
                    stop_column = ((left + center) / 2, (center + right) / 2)
            for row_index, anchor in enumerate(anchors):
                if anchor["text"] not in {"药疗", "药品", "用药"}:
                    continue
                middle = anchor["middle"]
                row_height = anchor["bottom"] - anchor["top"]
                top = (anchors[row_index - 1]["middle"] + middle) / 2 if row_index else middle - row_height
                bottom = (anchors[row_index + 1]["middle"] + middle) / 2 if row_index + 1 < len(anchors) else middle + row_height
                cells = [box for box in body_boxes if top < box["middle"] < bottom]

                def values(left, right):
                    return sorted((box for box in cells if in_column(box, left, right)),
                                  key=lambda box: box["left"])

                start_cells = values(0, kind_start)
                duration_cells = values(duration_start, content_start)
                content_cells = values(content_start, content_end)
                status_cells = values((content_end + status["left"]) / 2, status_end)
                start_text = " ".join(box["text"] for box in start_cells)
                dates = explicit_dates(start_text)
                if (len(dates) != 1 or dates[0]["precision"] != "DAY"
                        or not start_text.startswith(dates[0]["raw"])
                        or not duration_cells or not content_cells or not status_cells):
                    continue
                duration_text = " ".join(box["text"] for box in duration_cells)
                if duration_text not in {"长期", "临时"}:
                    continue
                content_text, transforms = _join_order_content(content_cells)
                lines = [
                    "开始时间：" + start_text,
                    duration_text + " " + content_text,
                    "执行状态：" + " ".join(box["text"] for box in status_cells),
                ]
                evidence_boxes = [start, kind, duration, content_header, status, anchor,
                                  *start_cells, *duration_cells, *content_cells, *status_cells]
                if stop_column:
                    stop_cells = values(*stop_column)
                    stop_text = " ".join(box["text"] for box in stop_cells)
                    if stop_text:
                        lines.append(stop_header["name"] + "：" + stop_text)
                        evidence_boxes.extend([stop_header, *stop_cells])
                output.append({
                    "category": FactCategory.TREATMENT, "page": page, "lines": lines,
                    "blocks": [box["block"] for box in evidence_boxes],
                    "heading": "药疗医嘱", "heading_end": 0,
                    "page_bounded": anchor["bottom"] + row_height / 2 >= 1.0,
                    "record_date": {"raw": "", "value": None, "precision": "UNKNOWN", "conflict": False},
                    "medication_order": True,
                    **({"transcription_transforms": transforms} if transforms else {}),
                })
    return output


def explicit_dates(text):
    output = []
    for match in _DATE.finditer(text):
        year, month, day = match.group(1, 2, 3)
        try:
            date(int(year), int(month or 1), int(day or 1))
        except ValueError:
            continue
        precision = "DAY" if day else "MONTH" if month else "YEAR"
        value = year + (f"-{int(month):02d}" if month else "") + (f"-{int(day):02d}" if day else "")
        item = {"raw": match.group().strip(), "value": value, "precision": precision}
        if item not in output:
            output.append(item)
    return output


def content_for(category, text, summary=None):
    dates = explicit_dates(text) if category == FactCategory.TREATMENT else []
    distinct = {item["value"] for item in dates}
    only = dates[0] if len(distinct) == 1 else None
    report_date = None
    if summary and (summary.document_date or summary.document_date_raw):
        value = str(summary.document_date) if summary.document_date else None
        report_date = {
            "raw": summary.document_date_raw, "value": value[:4] if value and summary.date_precision == "YEAR" else value[:7] if value and summary.date_precision == "MONTH" else value,
            "precision": summary.date_precision if value else "UNKNOWN",
        }
    return {
        "category": category, "text": text,
        "date": only["value"] if only else None, "date_raw": only["raw"] if only else "",
        "date_precision": only["precision"] if only else "UNKNOWN", "dates": dates,
        "record_date": report_date, "institution": summary.institution_raw if summary else "",
        "limitations": (
            (["multiple_explicit_dates"] if len(distinct) > 1 else ["event_date_unknown"] if category == FactCategory.TREATMENT and not dates else [])
            + (["uncertain_expression"] if re.search(r"考虑|可能|不确定|不详|未明确|待排|可疑", text) else [])
            + (["negated_expression"] if re.search(r"未见|未发现|未检出|无明确", text) else [])
        ),
    }


def _heading_info(line, document_type, *, patient_information=False):
    names = {name: category for category, values in _HEADINGS.items() for name in values}
    if patient_information:
        names["肿瘤类型"] = "DIAGNOSIS"
    if document_type in {"IMAGING", "PATHOLOGY"}:
        names.update({name: document_type for name in _GENERIC_CONCLUSIONS})
        names["诊断"] = document_type
    for name in sorted(names, key=len, reverse=True):
        match = re.match(r"^[【\[]?" + r"\s*".join(map(re.escape,name)) + r"[】\]]?\*?(?:\s*[:：]|\s*$)", line, re.IGNORECASE)
        if match:
            return names[name], match.end(), name
    return None


def section_candidates(blocks, document_type):
    """A section is bounded by the next heading and never spans source pages."""
    blocks = list(blocks)
    lines = list(source_lines(blocks))
    record_dates = page_record_dates(lines)
    qualifications = {}
    page_types = {}
    for page, text, line_blocks in lines:
        compact = re.sub(r"\s+", "", text)
        if re.search(r"(?:CT|MRI?|磁共振|超声).*诊断报告|影像表现[:：]|超声提示[:：]", compact, re.I):
            page_types[page] = "IMAGING"
        if "信息来自患者送检时提供信息" in compact or "信息由受检者送检时提供" in compact:
            qualifications[page] = (text, line_blocks)
    output, pending = [], None
    def finish(page_bounded=False):
        if pending:
            body = "\n".join(pending["lines"])[pending["heading_end"]:].strip(" \n:：")
            # Blank fields and slash placeholders do not become clinical conclusions.
            if body and not re.fullmatch(r"[/\\—-]+", body):
                if pending["heading"] != "现病史" or re.search(r"治疗|化疗|放疗|手术|口服|予以|曾用|术",body):
                    pending["page_bounded"] = page_bounded
                    if pending["heading"] in {"病理诊断","肿瘤类型"} and pending["page"] in qualifications:
                        qualifier, qualifier_blocks = qualifications[pending["page"]]
                        pending["category"] = "DIAGNOSIS"
                        if qualifier not in pending["lines"]:
                            pending["lines"].append(qualifier)
                            pending["blocks"].extend(block for block in qualifier_blocks if block not in pending["blocks"])
                    pending["record_date"] = record_dates.get(pending["page"])
                    output.append(pending)
    for page, line, line_blocks in lines:
        text = line.strip()
        if not text:
            continue
        # A date/signature column beside a wrapped diagnosis must not terminate that diagnosis.
        # Keep each clinical excerpt in the horizontal lane established by its actual heading box.
        bounds = None
        try:
            points = [point for block in line_blocks for point in normalized_polygon(
                getattr(block, "layout_polygon", None) or block.polygon
            )]
            bounds = min(point[0] for point in points), max(point[0] for point in points)
        except (InvalidRegion, TypeError):
            pass
        heading = _heading_info(text, page_types.get(page, document_type), patient_information=page in qualifications)
        if pending and pending["page"]==page and pending.get("lane") and bounds and bounds[0] > pending["lane"]+.005 and not heading:
            continue
        stop = _STOP.match(re.sub(r"\s+", "", text)) or _TABLE_STOP.match(text)
        # A document title in a provider's trailing text is a boundary, not a diagnosis continuation.
        footer = re.match(r"^(?:第\s*\d+\s*页|.*医院$|入\s*院\s*记\s*录$|病\s*程\s*记\s*录$)", text)
        if pending and (pending["page"] != page or heading or stop or footer):
            finish(page_bounded=pending["page"] != page or bool(footer))
            pending = None
        if heading:
            category, end, name = heading
            pending = {"category": category, "page": page, "lines": [], "blocks": [],
                       "heading": name, "heading_end": end,
                       "lane": max(bounds[1],bounds[0]+.25) if bounds else None}
        if pending:
            if pending["category"] == FactCategory.DIAGNOSIS:
                trailing = _INLINE_METADATA.search(text)
                if trailing:
                    text = text[:trailing.start()].rstrip()
            pending["lines"].append(text)
            pending["blocks"].extend(block for block in line_blocks if block not in pending["blocks"])
    finish(page_bounded=True)
    for order in _medication_rows(blocks):
        if order["page"] in record_dates:
            order["record_date"] = record_dates[order["page"]]
        output.append(order)
    return output


def extract_version_facts(version):
    """Idempotent for a frozen parse; failures remain visible without losing the original."""
    with transaction.atomic():
        # Pipeline callers hold the document lease lock; a separate retry serializes on the version.
        type(version).objects.select_for_update().get(pk=version.pk)
        existing = FactExtraction.objects.filter(parsing_version=version).first()
        if existing:
            return existing
        blocks = list(OcrBlock.objects.filter(parsing_version=version).select_related("document_page").order_by(
            "document_page__page_number", "reading_order", "pk",
        ))
        summary = getattr(version, "document_summary", None)
        sections = section_candidates(blocks, summary.document_type if summary else "UNKNOWN")
        for index, section in enumerate(sections):
            text = "\n".join(section["lines"])
            region = None
            # One existing block can provide a real region; multi-block excerpts use page fallback.
            if len(section["blocks"]) == 1:
                try:
                    region = normalized_polygon(section["blocks"][0].polygon)
                except (InvalidRegion, TypeError):
                    pass
            evidence = SourceEvidence.objects.create(
                parsing_version=version, document_page_id=section["page"],
                source_text=text, polygon=region,
                confidence=min(block.confidence for block in section["blocks"]),
            )
            content = content_for(section["category"], text, summary)
            if section.get("record_date") is not None:
                context = section["record_date"]
                content["record_date"] = {key:context[key] for key in ("raw","value","precision")}
                if context["conflict"]:
                    content["limitations"].append("record_date_conflict")
            if section["page_bounded"]:
                content["limitations"].append("page_bounded_excerpt")
            if section.get("medication_order"):
                content["limitations"].append("medication_order_transcription")
            if section.get("transcription_transforms"):
                content["transcription_transforms"] = section["transcription_transforms"]
                content["limitations"].append("overlapping_text_merged")
            fact = Fact(
                document_id=version.document_id, parsing_version=version,
                document_page_id=section["page"], evidence=evidence, origin="AUTOMATIC",
                category=section["category"], raw_text=text,
                automatic_content=content, reading_order=index,
            )
            fact.full_clean()
            fact.save()
        return FactExtraction.objects.create(
            parsing_version=version, status="EXTRACTED" if sections else "NO_CANDIDATES",
            candidate_count=len(sections), extractor_version=EXTRACTOR_VERSION,
            reason="" if sections else "no_explicit_sections" if blocks else "no_ocr",
        )
