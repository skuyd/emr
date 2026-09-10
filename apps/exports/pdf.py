"""A4 card layout with a measured main page and explicitly requested appendices."""

import io
from pathlib import Path
import threading
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.doctemplate import LayoutError

from .errors import PdfUnavailable


FONT = "PHRNotoSansSC"
_FONT_LOCK = threading.RLock()
_RENDER_LOCK = threading.RLock()
MARGIN = 34
WIDTH = A4[0] - MARGIN * 2 - 12
HEIGHT = A4[1] - MARGIN * 2 - 12
EMPTY = "当前所选资料中暂无可用信息（不表示无相关病史）。"


def _font():
    with _FONT_LOCK:
        if FONT not in pdfmetrics.getRegisteredFontNames():
            path = Path(settings.BASE_DIR) / "static" / "fonts" / "noto-sans-sc.ttf"
            if not path.is_file():
                raise PdfUnavailable("中文字体暂不可用，请稍后重新生成。")
            pdfmetrics.registerFont(TTFont(FONT, str(path)))
        return pdfmetrics.getFont(FONT)


def _text(value):
    value = str(value)
    missing = {ord(char) for char in value if not char.isspace() and ord(char) not in _font().face.charToGlyph}
    if missing:
        raise PdfUnavailable("所选内容含当前字体无法完整显示的字符，请调整内容或使用 JSON/CSV 导出。")
    return escape(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


def _p(text, *, heading=False):
    style = ParagraphStyle(
        "section" if heading else "body", fontName=FONT, fontSize=12 if heading else 10.5,
        leading=17 if heading else 15, alignment=TA_LEFT, wordWrap="CJK",
        textColor=colors.HexColor("#173b46") if heading else colors.HexColor("#182429"),
        spaceBefore=7 if heading else 0, spaceAfter=5, splitLongWords=True,
    )
    return Paragraph(_text(text), style)


def scope_text(snapshot):
    selection = snapshot["selection"]
    if selection.get("mode") == "dates":
        label = f'{selection["start"]} 至 {selection["end"]}（含首尾）'
        if selection.get("unknown_ids"):
            label += "，另含已选日期不确定资料"
    else:
        label = "本次确认的全部正常资料" if selection.get("mode") == "all" else "本次勾选资料"
    records = len(snapshot.get('self_records', []))
    glucose_count = len(snapshot.get('glucose_records', []))
    treatment_count = len(snapshot.get("treatment_events", [])) + len(snapshot.get("treatment_cycles", []))
    statement_count = len(snapshot.get('cancer_candidates', []))
    return (f'{label}，共 {len(snapshot["documents"])} 份' + (f'，另含明确勾选的 {records} 条日常记录' if records else '')
            + (f'，另含明确勾选的 {glucose_count} 条血糖记录' if glucose_count else '')
            + (f'，另含 {len(snapshot.get("cloud_imaging_sources", []))} 条选定云影像来源' if snapshot.get('cloud_imaging_sources') else '')
            + (f'，另含 {treatment_count} 项治疗事件或周期' if treatment_count else '')
            + (f'，另含 {statement_count} 条选定报告表述' if statement_count else '')
            + ('，含当前指标显示偏好' if snapshot.get('indicator_ordering') else ''))


def card_sections(snapshot):
    """Text and row content used by both the HTML preview and the PDF."""
    from apps.cloud_imaging.projection import assert_safe_snapshot

    assert_safe_snapshot(snapshot)
    labels = {row["id"]: f"D{index:02d}" for index, row in enumerate(snapshot["documents"], 1)}
    documents = {row["id"]: row for row in snapshot["documents"]}
    index_included = any(row["key"] == "sources" and row["included"] for row in snapshot["card"]["sections"])
    def source(row):
        if not index_included:
            document = documents[row["document_id"]]
            return f'{document["filename"]}（资料编号 {document["id"]}）第 {row["page"]} 页'
        return f'{labels[row["document_id"]]} 第 {row["page"]} 页'

    sections = []
    for section in snapshot["card"]["sections"]:
        entries = []
        key = section["key"]
        if not section["included"]:
            entries.append({"text": "本次未选择纳入。"})
        elif key == "patient":
            entries.append({"text": "姓名或昵称：" + snapshot["patient"]["nickname"]})
            if snapshot["patient"]["basic_info"]:
                entries.append({"text": snapshot["patient"]["basic_info"]})
        elif key in {"diagnosis", "treatment", "imaging"}:
            for row in snapshot["card"]["groups"][key]:
                content = row["content"]
                time = ""
                if key == "treatment":
                    dates = content.get("date_raw") or "、".join(item["raw"] for item in content.get("dates", []))
                    if "multiple_explicit_dates" in content.get("limitations", []):
                        time = f"原文提及多个日期：{dates}（未逐项关联治疗，请结合摘录核对）；"
                    else:
                        time = f'原文日期：{dates or "原文未记载明确时间"}；'
                else:
                    recorded = content.get("record_date") or {}
                    time = f'记录日期：{recorded.get("raw") or recorded.get("value") or "未明确"}；'
                limitation = "不同来源记载存在差异；" if row.get("report_differences") else ""
                if row.get("conflict"):
                    limitation += "同一字段存在多个已核对值；"
                if "page_bounded_excerpt" in content.get("limitations", []):
                    limitation += "摘录止于本页，请核对是否有续文；"
                if "record_date_conflict" in content.get("limitations", []):
                    limitation += "本页报告日期存在冲突；"
                entries.append({"text": f'{time}{limitation}{content["text"]} [{source(row["source"])}]'})
        elif key == 'cancer_ordering':
            from apps.cancer_ordering.output import card_entries
            entries.extend(card_entries(snapshot))
        elif key == "labs":
            labs = {row["id"]: row for row in snapshot["labs"]}
            for identity in snapshot["card"]["lab_ids"]:
                row = labs[identity]
                date = row["date"]["raw"] or row["date"]["value"] or "日期未明确"
                notes = "；".join(issue.get("label", issue["code"]) for issue in row["quality_issues"])
                detail = f'{date}；{source(row)}；{row["comparison"]["label"]}'
                detail += f'；{row["specimen"] or "标本未记载"}；{row["method"] or "方法未记载"}'
                field_pages = sorted({value.get("page_number") for value in row["field_evidence"].values()
                                      if value.get("page_number") and value["page_number"] != row["page"]})
                if field_pages:
                    detail += "；字段来源另见 " + labels[row["document_id"]] + " 第 " + "、".join(map(str, field_pages)) + " 页"
                if notes:
                    detail += "；" + notes
                entries.append({"cells": [row["standard_name"] or row["name"], row["value"] or "未记载",
                                          row["unit"] or "单位未记载", row["reference_range_raw"] or "参考范围未记载"],
                                "text": detail})
            for series in snapshot["card"]["trends"]:
                points = series["points"][-4:]
                entries.append({"text": series["standard_name"] + "可比历史（最近 " + str(len(points)) + " 次）：" +
                                " → ".join(f'{point["date"]} {point["value"]}' for point in points)
                                + f' {series["unit"]}；{series["basis"]}。仅陈列数值变化。'})
        elif key == "self_records":
            chosen = set(snapshot['card'].get('self_record_ids', []))
            for row in snapshot.get('self_records', []):
                if row['id'] not in chosen:
                    continue
                data = row['data']
                value = f"{data['raw_value']} {data['raw_unit']}" if row['kind'] != 'SYMPTOM' else ' · '.join(
                    item for item in (data['symptom_name'], data['severity']) if item)
                conversion = ''
                if row['kind'] != 'SYMPTOM':
                    conversion = f"换算值：{data['normalized_value']} {data['normalized_unit']}（{data['conversion']['formula']}）；"
                entries.append({'text': f"{row['kind_label']}：{value}；{data['local_time']}，{data['timezone']}（UTC{data['utc_offset']}）；"
                                f"时间精度：分钟；{conversion}"
                                f"测量方式：{data['source_label'] or '未填写'}；备注：{data['notes'] or '未填写'}；"
                                f"记录人：{row.get('created_by') or '已注销账号'}；最近修改人：{row.get('updated_by') or '已注销账号'}；"
                                f"记录编号 {row['id']}，修订 {row['revision_number']}。"})
        elif key == "glucose":
            from apps.glucose.output import card_entries
            entries.extend(card_entries(snapshot))
        elif key == "cloud_imaging":
            from apps.cloud_imaging.output import card_entries
            entries.extend(card_entries(snapshot))
        elif key == "sources":
            for document in snapshot["documents"]:
                entries.append({"text": f'{labels[document["id"]]} {document["filename"]}；共 {document["page_count"]} 页；'
                                f'资料日期：{document["date_raw"] or document["date"] or "未明确"}'})
        if not entries:
            entries.append({"text": EMPTY})
        sections.append({**section, "entries": entries})
    from .treatment_card import card_sections as treatment_sections
    from apps.lesions.output_presentation import card_sections as lesion_sections
    return [*sections, *treatment_sections(snapshot), *lesion_sections(snapshot)]


def _flow(entry):
    if "cells" not in entry:
        return [_p(entry["text"])]
    table = Table(
        [[_p(value) for value in ("指标", "结果", "单位", "参考范围")],
         [_p(value) for value in entry["cells"]]],
        colWidths=[WIDTH * value for value in (.25, .23, .20, .32)],
        repeatRows=1, splitByRow=1, splitInRow=1,
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3f2")),
        ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#cad6d4")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [table, Spacer(1, 3), _p(entry["text"])]


def _height(flows):
    return sum(flow.wrap(WIDTH, 1000000)[1] + flow.getSpaceBefore() + flow.getSpaceAfter() for flow in flows)


def _footer(canvas, document):
    canvas.setFont(FONT, 9)
    canvas.setFillColor(colors.HexColor("#465653"))
    canvas.drawString(MARGIN, 19, "依据所选资料整理；请结合原件阅读。")
    canvas.drawRightString(A4[0] - MARGIN, 19, f"第 {document.page} 页")


def render_pdf(snapshot):
    # ReportLab mutates shared font subset state while building a document.
    with _RENDER_LOCK:
        _font()
        sections = card_sections(snapshot)
        candidate_sections = [section for section in sections if section.get("appendix_only")]
        sections = [section for section in sections if not section.get("appendix_only")]
        if candidate_sections and not snapshot["card"]["details"]:
            raise PdfUnavailable("候选内容只放附页，请明确选择允许附页后重新预览。")
        header = [_p("就诊速查卡", heading=True),
                  _p("内容生成时间：" + snapshot["generated_at"] + "\n范围：" + scope_text(snapshot))]
        flows = header.copy()
        for section in sections:
            flows.append(_p(section["title"], heading=True))
            for entry in section["entries"]:
                flows.extend(_flow(entry))
        if _height(flows) > HEIGHT - 8:
            if not snapshot["card"]["details"]:
                raise PdfUnavailable("内容超出 A4 一页。请减少所选内容，或明确选择“允许附页”后重新预览。")
            flows = header + [_p("部分明细见附页；主页面保留各部分入口及来源编号。")]
            appendix = []
            for index, section in enumerate(sections):
                flows.append(_p(section["title"], heading=True))
                # Reserve every remaining section heading and its continuation notice.
                reserve = sum(_height([_p(item["title"], heading=True), _p("详见附页。")]) for item in sections[index + 1:])
                omitted = []
                for entry in section["entries"]:
                    candidate = _flow(entry)
                    if omitted or _height(flows + candidate + [_p("更多内容见附页。")]) + reserve > HEIGHT - 8:
                        omitted.append(entry)
                    else:
                        flows.extend(candidate)
                if omitted:
                    flows.append(_p("更多内容见附页。"))
                    appendix.append(_p(section["title"] + "（附页）", heading=True))
                    for entry in omitted:
                        appendix.extend(_flow(entry))
            if _height(flows) > HEIGHT - 8:
                raise PdfUnavailable("基本信息或范围说明过长，请调整后重新预览。")
            flows.extend([PageBreak(), _p("速查卡明细附页", heading=True), *appendix])
        if candidate_sections:
            flows.extend([PageBreak(), _p("候选附页：下列内容尚待核对确认", heading=True)])
            for section in candidate_sections:
                flows.append(_p(section["title"], heading=True))
                for entry in section["entries"]:
                    flows.extend(_flow(entry))
        output = io.BytesIO()
        doc = SimpleDocTemplate(
            output, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
            title="就诊速查卡", author="健康之家", pageCompression=1,
        )
        try:
            doc.build(flows, onFirstPage=_footer, onLaterPages=_footer)
        except LayoutError:
            raise PdfUnavailable("所选内容无法完整排入当前版面，请调整选择后重新预览。") from None
        if doc.page > 1 and not snapshot["card"]["details"]:
            raise PdfUnavailable("内容超出 A4 一页，请调整选择或允许附页。")
        return output.getvalue()
