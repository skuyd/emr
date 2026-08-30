from dataclasses import dataclass, replace
from datetime import date
import re

from .models import DatePrecision, DocumentType, MetadataKind
from .value_objects import OcrPage


_DATE = re.compile(
    r"(?P<year>(?:19|20)\d{2})(?:(?:[-/.](?P<month>\d{1,2})(?:[-/.](?P<day>\d{1,2}))?)|"
    r"(?:年(?:(?P<month_cn>\d{1,2})月(?:(?P<day_cn>\d{1,2})日?)?)?))"
)
_DATE_LABEL = re.compile(r"采样日期|采样时间|检查日期|检验日期|报告日期|签署日期|取材日期|治疗日期|日期")
_INSTITUTION = re.compile(r"医院|医学中心|检验中心|检测中心|诊所|卫生院")
_CLASSIFIERS = (
    (DocumentType.PATHOLOGY, ("病理报告", "病理诊断", "免疫组化")),
    (DocumentType.DISCHARGE, ("出院小结", "出院记录")),
    (DocumentType.IMAGING, ("影像报告", "放射报告", "超声报告", "CT报告", "MRI报告")),
    (DocumentType.ORDER, ("医嘱", "处方")),
    (DocumentType.TREATMENT, ("治疗记录", "放疗记录", "化疗记录")),
    (DocumentType.LAB, ("检验报告", "检验结果", "化验报告", "血常规")),
)


@dataclass(frozen=True)
class MetadataCandidateValue:
    kind: str
    raw_text: str
    normalized_value: str
    precision: str
    confidence: float
    page_number: int | None
    region: tuple[tuple[float, float], ...] | None
    selected: bool = False
    rationale: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExtractedDocumentMetadata:
    document_type: str
    document_date_raw: str
    document_date: date | None
    date_precision: str
    institution_raw: str
    confidence: float
    candidates: tuple[MetadataCandidateValue, ...]


def _date_value(match):
    year = int(match.group("year"))
    month_text = match.group("month") or match.group("month_cn")
    day_text = match.group("day") or match.group("day_cn")
    if month_text is None:
        return date(year, 1, 1), DatePrecision.YEAR, f"{year:04d}"
    month = int(month_text)
    if day_text is None:
        return date(year, month, 1), DatePrecision.MONTH, f"{year:04d}-{month:02d}"
    day = int(day_text)
    return date(year, month, day), DatePrecision.DAY, f"{year:04d}-{month:02d}-{day:02d}"


def _date_priority(raw_text, document_type):
    if document_type == DocumentType.LAB:
        if any(label in raw_text for label in ("采样日期", "采样时间", "检查日期", "检验日期")):
            return 0
        if "报告日期" in raw_text:
            return 1
    else:
        if any(label in raw_text for label in ("检查日期", "取材日期", "治疗日期")):
            return 0
        if any(label in raw_text for label in ("报告日期", "签署日期")):
            return 1
    return 2


def _classify(pages, observation_count):
    if observation_count:
        return DocumentType.LAB, 0.99, None
    for page in pages:
        for region in page.regions:
            for document_type, keywords in _CLASSIFIERS:
                if any(keyword.casefold() in region.text.casefold() for keyword in keywords):
                    return document_type, 0.90, (page, region)
    if any(page.regions for page in pages):
        return DocumentType.OTHER, 0.50, None
    return DocumentType.UNKNOWN, 0.20, None


def _classification_candidate(document_type, confidence, evidence):
    page_number = evidence[0].page_number if evidence else None
    region = evidence[1].polygon if evidence else None
    raw_text = evidence[1].text if evidence else ""
    return MetadataCandidateValue(
        kind=MetadataKind.DOCUMENT_TYPE,
        raw_text=raw_text,
        normalized_value=document_type,
        precision=DatePrecision.UNKNOWN,
        confidence=confidence,
        page_number=page_number,
        region=region,
        selected=True,
        rationale=(("rule", "document_type_classifier"),),
    )


def _date_candidates(pages, document_type):
    values = []
    for page in pages:
        for region in page.regions:
            if _DATE_LABEL.search(region.text) is None:
                continue
            for match in _DATE.finditer(region.text):
                try:
                    parsed, precision, normalized = _date_value(match)
                except ValueError:
                    continue
                priority = _date_priority(region.text, document_type)
                values.append(
                    MetadataCandidateValue(
                        kind=MetadataKind.DOCUMENT_DATE,
                        raw_text=region.text.strip(),
                        normalized_value=normalized,
                        precision=precision,
                        confidence=max(0.50, 0.98 - priority * 0.15),
                        page_number=page.page_number,
                        region=region.polygon,
                        rationale=(("priority", str(priority)),),
                    )
                )
    if not values:
        return (), None
    priorities = [int(dict(item.rationale)["priority"]) for item in values]
    best_priority = min(priorities)
    best = [item for item in values if int(dict(item.rationale)["priority"]) == best_priority]
    identities = {(item.normalized_value, item.precision) for item in best}
    if len(identities) != 1:
        return tuple(values), None
    selected_index = values.index(best[0])
    values[selected_index] = replace(values[selected_index], selected=True)
    selected = values[selected_index]
    parsed, _precision, _normalized = _date_value(_DATE.search(selected.raw_text))
    return tuple(values), (selected, parsed)


def _institution_candidates(pages):
    values = []
    for page in pages:
        for region in page.regions:
            raw = region.text.strip()
            if 2 <= len(raw) <= 96 and _INSTITUTION.search(raw):
                values.append(
                    MetadataCandidateValue(
                        kind=MetadataKind.INSTITUTION,
                        raw_text=raw,
                        normalized_value=raw,
                        precision=DatePrecision.UNKNOWN,
                        confidence=0.80,
                        page_number=page.page_number,
                        region=region.polygon,
                        selected=not values,
                        rationale=(("rule", "institution_suffix"),),
                    )
                )
    return tuple(values)


def extract_document_metadata(pages, *, observation_count=0):
    pages = tuple(pages)
    if any(not isinstance(page, OcrPage) for page in pages):
        raise ValueError("Metadata extraction requires OCR pages")
    document_type, type_confidence, type_evidence = _classify(pages, observation_count)
    classification = _classification_candidate(document_type, type_confidence, type_evidence)
    dates, selected_date = _date_candidates(pages, document_type)
    institutions = _institution_candidates(pages)
    if selected_date is None:
        raw_date, parsed_date, precision = "", None, DatePrecision.UNKNOWN
    else:
        selected, parsed_date = selected_date
        raw_date, precision = selected.raw_text, selected.precision
    institution = next((item.raw_text for item in institutions if item.selected), "")
    return ExtractedDocumentMetadata(
        document_type=document_type,
        document_date_raw=raw_date,
        document_date=parsed_date,
        date_precision=precision,
        institution_raw=institution,
        confidence=type_confidence,
        candidates=(classification, *dates, *institutions),
    )
