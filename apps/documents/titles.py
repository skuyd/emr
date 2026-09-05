"""Display names derived from the active recognition result; originals stay immutable."""

from collections import defaultdict
import re
import unicodedata

from django.db.models import Count, Exists, OuterRef, Prefetch, Q

from apps.labs.dictionary import default_dictionary
from apps.labs.models import LabObservation
from apps.labs.quality import MIN_OBSERVATION_CONFIDENCE, MIN_STANDARD_NAME_CONFIDENCE
from apps.processing.models import DocumentType, OcrBlock


_EXAM_ALIASES = (
    ("血常规", ("血常规", "血细胞分析", "全血细胞计数")),
    ("生化", ("生化",)),
    ("尿常规", ("尿常规", "尿液分析")),
    ("凝血功能", ("凝血功能", "凝血常规", "凝血四项", "凝血五项", "凝血六项")),
    ("肝功能", ("肝功能",)),
    ("肾功能", ("肾功能",)),
    ("血脂", ("血脂",)),
    ("电解质", ("电解质",)),
    ("甲状腺功能", ("甲状腺功能", "甲功")),
    ("肿瘤标志物", ("肿瘤标志物",)),
)
_EXAM_NAMES = tuple(
    (name, re.compile("|".join(re.escape(alias) for alias in aliases)))
    for name, aliases in _EXAM_ALIASES
)
_CATEGORY_NAMES = {
    "HEMATOLOGY": "血常规",
    "BIOCHEMISTRY": "生化",
    "COAGULATION": "凝血功能",
    "TUMOR_MARKER": "肿瘤标志物",
    "IMMUNOLOGY": "免疫检验",
    "SEROLOGY": "血清学检验",
}
_HEADING_LABEL = re.compile(r"^(?:检验项目|检查项目|项目名称|报告名称|检查名称)[:：]")
_HEADING_DECORATION = re.compile(r"检验|检查|检测|分析|报告单?|结果|全套|常规|组合|[三五]分类|\d+项")


def with_title_evidence(versions):
    """Two batched reads, independent of the number of document cards."""
    return versions.prefetch_related(
        Prefetch(
            "ocr_blocks",
            queryset=OcrBlock.objects.filter(confidence__gte=MIN_STANDARD_NAME_CONFIDENCE)
            .only("parsing_version_id", "text", "confidence")
            .order_by("document_page__page_number", "reading_order", "pk"),
            to_attr="title_ocr_blocks",
        ),
        Prefetch(
            "lab_observations",
            queryset=LabObservation.objects.filter(evidence__confidence__gte=MIN_STANDARD_NAME_CONFIDENCE)
            .select_related("evidence")
            .only("parsing_version_id", "standard_code", "evidence__confidence")
            .order_by("document_page__page_number", "reading_order", "pk"),
            to_attr="title_observations",
        ),
    )


def title_search_q(query):
    """Find the source aliases and indicator groups behind a displayed exam name."""
    query = re.sub(r"\s+", "", unicodedata.normalize("NFKC", query))
    result = Q()
    for name in query.removesuffix("等检查").split("、"):
        match = _exam_search_q(name)
        if not match:
            return Q()
        result &= match
    return result


def _exam_search_q(query):
    if not query:
        return Q()
    active = {"parsing_version__document_id": OuterRef("pk"), "parsing_version__active": True}
    result = Q()
    patterns = [
        r"\s*".join(re.escape(character) for character in alias)
        for name, aliases in _EXAM_ALIASES if query in name
        for alias in aliases
    ]
    if patterns:
        # OCR may insert spaces between Chinese characters in a heading.
        result |= Q(Exists(OcrBlock.objects.filter(
            **active, confidence__gte=MIN_STANDARD_NAME_CONFIDENCE, text__iregex="|".join(patterns),
        )))
    for category, name in _CATEGORY_NAMES.items():
        if query not in name:
            continue
        codes = [item.code for item in default_dictionary().indicators if item.category == category]
        observations = (
            LabObservation.objects.filter(
                **active, standard_code__in=codes, evidence__confidence__gte=MIN_STANDARD_NAME_CONFIDENCE,
            )
            .order_by().values("parsing_version_id")
            .annotate(indicator_count=Count("standard_code", distinct=True))
            .filter(indicator_count__gte=2)
        )
        result |= Q(Exists(observations))
    return result


def _heading_names(text):
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if len(text) > 100:
        return ()
    text = _HEADING_LABEL.sub("", text)
    matches = []
    remaining = text
    for name, pattern in _EXAM_NAMES:
        match = pattern.search(text)
        if match:
            matches.append((match.start(), name))
            remaining = pattern.sub("", remaining)
    remaining = _HEADING_DECORATION.sub("", remaining).strip("()（）[]【】+、,，/|:：-—")
    # Only accept a heading made up of examination names and report labels.
    # Narrative mentions such as “建议复查血常规” are not performed examinations.
    return tuple(name for _, name in sorted(matches)) if not remaining else ()


def document_title(document, version, *, blocks=None, observations=None):
    if version is None or not version.active:
        return document.display_filename
    summary = getattr(version, "document_summary", None)
    document_type = summary.document_type if summary is not None else DocumentType.UNKNOWN
    if document_type in {DocumentType.LAB, DocumentType.UNKNOWN, DocumentType.OTHER}:
        blocks = version.title_ocr_blocks if blocks is None else blocks
        names = []
        for block in blocks:
            if block.confidence >= MIN_STANDARD_NAME_CONFIDENCE:
                for line in block.text.splitlines():
                    names.extend(_heading_names(line))
        if not names:
            observations = version.title_observations if observations is None else observations
            categories = {item.code: item.category for item in default_dictionary().indicators}
            groups = defaultdict(set)
            for observation in observations:
                if observation.evidence.confidence >= MIN_STANDARD_NAME_CONFIDENCE:
                    category = categories.get(observation.standard_code)
                    if category in _CATEGORY_NAMES:
                        groups[category].add(observation.standard_code)
            names = [_CATEGORY_NAMES[category] for category, codes in groups.items() if len(codes) >= 2]
        if names:
            names = list(dict.fromkeys(names))
            return "、".join(names[:3]) + ("等检查" if len(names) > 3 else "")
    if (
        summary is not None and summary.confidence >= MIN_OBSERVATION_CONFIDENCE
        and document_type not in {DocumentType.UNKNOWN, DocumentType.OTHER}
    ):
        return summary.get_document_type_display()
    return document.display_filename
