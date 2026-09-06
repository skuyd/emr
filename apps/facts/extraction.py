"""Literal section extraction. It does not infer diagnoses, stages or treatment cycles."""

from datetime import date
import re

from django.db import transaction

from apps.processing.models import OcrBlock, SourceEvidence
from apps.processing.value_objects import InvalidRegion, normalized_polygon

from .models import Fact, FactCategory, FactExtraction


EXTRACTOR_VERSION = "literal-sections-1"
_HEADINGS = {
    "DIAGNOSIS": ("出院诊断", "入院诊断", "临床诊断", "主要诊断", "初步诊断", "诊断"),
    "STAGE": ("临床分期", "病理分期", "TNM分期", "TNM 分期", "分期"),
    "TREATMENT": ("治疗经过", "诊疗经过", "治疗方案", "化疗方案", "药物治疗", "放疗记录", "放疗方案", "手术名称", "手术记录"),
    "IMAGING": ("影像学诊断", "影像诊断", "影像结论", "影像检查结论"),
    "PATHOLOGY": ("病理诊断", "病理结论", "病理检查结论"),
}
_GENERIC_CONCLUSIONS = ("诊断意见", "诊断结论", "检查结论", "检查印象", "结论", "印象")
_STOP = re.compile(r"^(?:检查所见|影像所见|肉眼所见|镜下所见|出院医嘱|医嘱|报告日期|检查日期|报告医师|审核医师|签名|送检材料|标本类型|临床资料|现病史|既往史|检验结果)\s*[:：]")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2})\s*日?)?\s*月?|年)(?!\d)")


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


def _heading(line, document_type):
    names = {name: category for category, values in _HEADINGS.items() for name in values}
    if document_type in {"IMAGING", "PATHOLOGY"}:
        names.update({name: document_type for name in _GENERIC_CONCLUSIONS})
        names["诊断"] = document_type
    for name in sorted(names, key=len, reverse=True):
        if re.match(r"^" + re.escape(name) + r"(?:\s*[:：]|\s*$)", line, re.IGNORECASE):
            return names[name]
    return None


def section_candidates(blocks, document_type):
    """A section is bounded by the next heading and never spans source pages."""
    output, pending = [], None
    for block in blocks:
        for line in block.text.splitlines():
            text = line.strip()
            if not text:
                continue
            category = _heading(text, document_type)
            if pending and (pending["page"] != block.document_page_id or category or _STOP.match(text)):
                output.append(pending)
                pending = None
            if category:
                pending = {"category": category, "page": block.document_page_id, "lines": [], "blocks": []}
            if pending:
                pending["lines"].append(text)
                if block not in pending["blocks"]:
                    pending["blocks"].append(block)
    if pending:
        output.append(pending)
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
            fact = Fact(
                document_id=version.document_id, parsing_version=version,
                document_page_id=section["page"], evidence=evidence, origin="AUTOMATIC",
                category=section["category"], raw_text=text,
                automatic_content=content_for(section["category"], text, summary), reading_order=index,
            )
            fact.full_clean()
            fact.save()
        return FactExtraction.objects.create(
            parsing_version=version, status="EXTRACTED" if sections else "NO_CANDIDATES",
            candidate_count=len(sections), extractor_version=EXTRACTOR_VERSION,
            reason="" if sections else "no_explicit_sections" if blocks else "no_ocr",
        )
