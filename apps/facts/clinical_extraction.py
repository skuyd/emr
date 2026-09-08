"""Literal imaging field candidates from complete, bounded OCR reports."""

from dataclasses import dataclass
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.patients.models import Patient
from apps.processing.models import OcrBlock, SourceEvidence

from .clinical_schema import SCHEMA_VERSION, field_content
from .clinical_segments import ReportText, SEGMENTER_VERSION, segment_reports
from .extraction import explicit_dates
from .models import ClinicalExtraction, ClinicalReport, ClinicalReportSpan, Fact, FactSourceFragment
from .readmodels import digest


EXTRACTOR_VERSION = "clinical-imaging-v6"
EXAM_DATE = re.compile(r"检查(?:日期|时间)[:：]?((?:19|20)\d{2}(?:[-/.年]\d{1,2})?(?:[-/.月]\d{1,2}日?)?)")
BODY = re.compile(r"检查(?:项目|名称|部位)[:：]?(.*?)(?=影像(?:表现|所见|描述)|检查所见|超声所见|临床诊断|告知|诊断(?:意见|提示)|(?:检查|扫描|送检|申请|报告)(?:日期|时间)|申请(?:科室|医生)|姓名|性别|年龄|门诊号|住院号|病历号|床号|$)")
FINDINGS = re.compile(r"(?:影像(?:表现|所见|描述)|检查所见|超声所见)[:：]?")
IMPRESSION = re.compile(r"(?:影像(?:诊断|结论)|诊断(?:意见|提示)|超声提示)[:：]?")
FOOTER = re.compile(r"(?:报告(?:日期|时间|医[师生])|审核(?:日期|时间|医[师生])|核片医[师生]|检查技师|本报告仅|此报告仅|注[:：])")
UNIT = r"(?:mm|cm|毫米|厘米)"
NUMBER = r"\d+(?:\.\d+)?"
DIMENSION = re.compile(rf"(?<![\d.]){NUMBER}(?:{UNIT})?(?:[×xX*]{NUMBER}(?:{UNIT})?){{0,2}}{UNIT}(?![A-Za-z])", re.I)
FOCAL = re.compile(r"结节|肿块|占位|囊肿|囊性(?:灶|无强化)|淋巴结|低密度影|异常信号影|致密影|液性暗区|等回声堆积|低回声区|软组织(?:密度影|信号|灶)|点状致密")
NEGATIVE_MODIFIER = r"(?:明显|明确|显著|异常|局部|稍|轻度|肿大|肿|增大|增粗|增厚|强化|放射性|浓聚|摄取)"
NEGATIVE = re.compile(
    rf"(?:未见|不见|无){NEGATIVE_MODIFIER}*"
    rf"(?:(?:{FOCAL.pattern})?(?:以及|及|和|或|与|、){NEGATIVE_MODIFIER}*)*$"
)
NONENHANCING_MODIFIER = re.compile(r"无(?:明显|明确|显著)?强化")
OBSERVATION_VERB = re.compile(r"可见|显示|见")
AFFIRMATIVE_OBSERVATION = re.compile(
    r"(?:(?:其)?(?:内|中|旁)|局部|上极|下极)?"
    r"(?:仍|尚|又|另|并|能够|能|(?:明确|明显|显著|清楚|清晰|充分)(?:地)?)*"
    r"(?:可见|显示|见)"
)
ANATOMICAL_SIZE = re.compile(r"(?:胆囊大小|脾(?:脏)?(?:长|厚)|(?:胆|胰|静脉|动脉)管[^。；]{0,16}|管径)[^。；]{0,12}$")
SITE = re.compile(
    r"(?:左|右|双)(?:侧)?(?:肺[上下中]叶(?:[上下]?舌段|尖后段|[前后背内外]段|[前后内外]基底段|基底段)?|肺(?:尖|门)?|肾(?:盂|窦|实质)?|肾上腺|乳(?:腺|房)?|额叶|颞叶|顶叶|枕叶|半卵圆中心)"
    r"|胰头(?:与胰体间|钩突|周围)?|胰(?:体|尾|腺)|肝(?:S\d+[a-z]?|[左右]叶|内|门部|包膜下)?"
    r"|腹(?:腔|膜后|主动脉周围)|纵隔(?:内隆突下)?|(?:胆囊腔内|胆囊窝|胆囊)|子宫(?:体部)?|骶管内"
    r"|[左右]额叶|[左右]颞叶|[左右]顶叶|[左右]枕叶|(?:颈|腋|腹股沟)部|S\d+[a-z]?|[左右]叶",
    re.I,
)
NEW_ANATOMICAL_STATEMENT = re.compile(
    rf"[,，](?=(?:但是|然而|但|而|另见|另外|同时)?(?:增强(?:扫描)?后)?(?P<site>{SITE.pattern}))", re.I,
)
CHAPTER_SUFFIX = re.compile(r"(?:外)?(?:评估|评价|检查|情况|所见)[:：]")
RELATIVE_SITE = re.compile(r"S\d+[a-z]?|[左右]叶", re.I)
SITE_CONNECTOR = re.compile(r"(?:[（(][0-9A-Za-z、,，区组站段]+[）)])?[区内旁]*(?:及|与|和|、)")
PAIRED_SITE = re.compile(r"(?P<side>左|右|双)(?:侧)?(?P<family>肾上腺|肺|肾|乳|额叶|颞叶|顶叶|枕叶|半卵圆中心)")


@dataclass
class Candidate:
    key: str
    entity: str
    value: dict
    fragments: list
    raw_value: str
    limitations: tuple = ()
    transformations: tuple = ()
    # Transient matching-view bounds; persisted fragments still use the raw
    # OCR Unicode string and its original page polygon.
    start: int = 0
    end: int = 0


def _candidate(view, key, value, start, end, *, entity="report", raw_value=None, limitations=(), transformations=()):
    raw = view.raw(start, end)
    return Candidate(key, entity, value, view.fragments(start, end), raw_value or raw, tuple(limitations), tuple(transformations), start, end)


def _finding_clauses(text):
    """Ranges in the matching view; raw Unicode offsets stay in ReportText.

    NFKC changes a fullwidth semicolon into ASCII. A comma starts a new local
    assertion only when it explicitly introduces anatomy, optionally after an
    enhancement-phase prefix. Measurement labels, time-role continuations and
    conjunctions within a named group stay together.
    """
    for sentence in re.finditer(r"[^。;；]+[。;；]?", text):
        # A relative liver segment/leaf still needs the explicitly named organ
        # in the same sentence for the existing audited textual composition.
        cuts = [0, *[match.end() for match in NEW_ANATOMICAL_STATEMENT.finditer(sentence.group())
                     if not RELATIVE_SITE.fullmatch(match.group("site"))], len(sentence.group())]
        for left, right in zip(cuts, cuts[1:]):
            if left < right:
                yield sentence.start() + left, sentence.start() + right


def _affirmative_observation(local):
    # The property exception needs a complete affirmative predicate after its
    # named anatomy or an explicit new assertion. Unknown lead-ins are not
    # evidence of affirmation; in particular, never accept a verb substring
    # from an unrecognized negative/inability expression.
    local = re.split(r"但是|然而|不过|但|而", local)[-1]
    verbs = list(OBSERVATION_VERB.finditer(local))
    if not verbs:
        return False
    verb = verbs[-1]
    sites = list(SITE.finditer(local[:verb.start()]))
    start = sites[-1].end() if sites else 0
    return AFFIRMATIVE_OBSERVATION.fullmatch(local[start:verb.end()]) is not None


def _focal_negated(prefix):
    negative = NEGATIVE.search(prefix)
    if negative is None:
        return False
    if NONENHANCING_MODIFIER.fullmatch(negative.group()):
        # A locally observed "nonenhancing focus" exists in the report. The
        # enhancement property is negative, not the focus. A negative latest
        # observation predicate ("not seen") must still exclude that focus.
        local = re.split(r"[，,。;；:：]", prefix[:negative.start()])[-1]
        if _affirmative_observation(local):
            return False
    return True


def _positive_focals(text):
    # A category label such as "淋巴结:" does not assert an abnormal finding.
    # Negation only matches its local suffix grammar, never the entire report.
    return [match for match in FOCAL.finditer(text)
            if text[match.end():match.end() + 1] not in {":", "："}
            and not _focal_negated(text[:match.start()])]


def _site(text, *, measured=False):
    matches = [match for match in SITE.finditer(text) if not CHAPTER_SUFFIX.match(text[match.end():])]
    if not measured:
        focal = _positive_focals(text)
        if not focal:
            return None
        # Bind location to the affirmative finding, not to the first organ in
        # its chapter or the last unrelated anatomical statement in a sentence.
        anchor = focal[0].start()
        postposed = re.match(r"(?:位于|位在|见于)", text[focal[0].end():])
        if postposed:
            start = focal[0].end() + postposed.end()
            following = [match for match in matches if match.start() >= start]
            if following and following[0].start() == start:
                anchor = following[0].end()
                for later in following[1:]:
                    if not SITE_CONNECTOR.fullmatch(text[anchor:later.start()]):
                        break
                    anchor = later.end()
        matches = [match for match in matches if match.end() <= anchor]
    if not matches:
        return None
    match = matches[-1]
    start, end = match.span()
    for earlier in reversed(matches[:matches.index(match)]):
        separator = text[earlier.end():start]
        if SITE_CONNECTOR.fullmatch(separator):
            start = earlier.start()
        else:
            break
    site = text[start:end]
    # A relative segment/leaf is only combined with an explicitly named organ
    # in this same source clause, and that textual composition is auditable.
    transformations = ()
    if RELATIVE_SITE.fullmatch(site) and any(m.group().startswith("肝") for m in matches[:-1]):
        site = "肝" + site
        transformations = ({"rule": "same_clause_explicit_organ_and_segment", "raw": text, "value": site},)
    return site, transformations


def _laterality(site):
    """A group side must describe every member of one named paired anatomy.

    Mixed organs, unsided members and unlike named brain regions retain their
    full location text without an invented group side. A single explicit lobe
    keeps its existing literal side; left/right liver lobes are not a bilateral
    pair of organs.
    """
    members = list(SITE.finditer(site))
    if len(members) == 1 and re.fullmatch(r"(?:肝)?[左右]叶", site):
        return "LEFT" if "左" in site else "RIGHT"
    paired = [PAIRED_SITE.match(member.group()) for member in members]
    if not paired or any(member is None for member in paired):
        return None
    if len({member.group("family") for member in paired}) != 1:
        return None
    sides = {member.group("side") for member in paired}
    if "双" in sides or sides == {"左", "右"}:
        return "BILATERAL"
    return "LEFT" if sides == {"左"} else "RIGHT"


def _examination_scope(raw):
    value = re.sub(r"^彩色(?:多普勒)?超声检查[（(](?:常规|普通|局部|专项)[）)]", "", raw)
    parts = []
    for part in value.split("|"):
        method = re.search(r"磁共振|MRI?|PET[/／]?CT|CT", part, re.I)
        if method and method.start() > 0:
            # The exact ordered anatomical prefixes stay intact, including
            # nested site qualifiers. Technique suffixes remain in raw_value.
            part = part[:method.start()].strip()
        parts.append(part)
    value = "|".join(parts)
    scope = re.search(r"[（(]([^()（）]+)[）)]", value)
    anatomy = r"全身|胸|腹|盆|头|颅|颈|肝|胆|胰|脾|肾|乳|甲状腺|四肢"
    if scope and re.search(anatomy, scope.group(1)) and not re.search(anatomy, value[:scope.start()] + value[scope.end():]):
        value = scope.group(1)
    return value or raw


def _dimension_value(raw, context):
    components = []
    parts = re.split(r"[×xX*]", raw)
    units = [re.search(UNIT, part, re.I) for part in parts]
    inherited = next((unit.group() for unit in reversed(units) if unit), None)
    if inherited is None:
        return None
    explicit_axis = next((axis for marker, axis in [
        ("短径", "SHORT"), ("长径", "LONG"), ("直径", "DIAMETER"), ("宽", "WIDTH"),
        ("厚", "DEPTH"), ("高", "HEIGHT"),
    ] if marker in context[-10:]), None) if len(parts) == 1 else None
    for part, unit in zip(parts, units):
        number = re.match(NUMBER, part).group()
        components.append({"value": number, "unit": (unit.group() if unit else inherited), "axis": explicit_axis})
    return {"components": components, "approximate": "约" in context[-12:],
            "measurement_role": _measurement_role(context)[0], "raw": raw}


def _measurement_role(prefix):
    # Only explicit language immediately introducing this measurement changes
    # its time role. A general "compared with prior" header is not a date/value.
    marker = re.search(r"(原|前片|上次|既往|此前|现|本次|此次|目前)(?:大小|直径|长径|短径|测得|为|约|示|见)*$", prefix)
    if marker:
        return ("HISTORICAL" if marker.group(1) in {"原", "前片", "上次", "既往", "此前"} else "CURRENT"), True
    return "CURRENT", False


def field_candidates(segment):
    view = ReportText(segment.pieces)
    text = view.text
    output = []
    for match in EXAM_DATE.finditer(text):
        if FINDINGS.search(text[:match.start()]) or IMPRESSION.search(text[:match.start()]):
            continue
        dates = explicit_dates(match.group(1))
        if dates:
            value = {"value": dates[0]["value"], "precision": dates[0]["precision"]}
            output.append(_candidate(view, "report.exam_date", value, match.start(), match.end(), raw_value=view.raw(*match.span(1))))
    title = re.search(r"PET[/／]?CT|MRI?|CT|彩色超声|超声|磁共振|X线", text, re.I)
    if title:
        code = "PET_CT" if "PET" in title.group().upper() else "MR" if title.group().upper().startswith("MR") or title.group() == "磁共振" else "US" if "超声" in title.group() else "XRAY" if title.group() == "X线" else "CT"
        output.append(_candidate(view, "imaging.modality", {"code": code, "raw": view.raw(*title.span())}, *title.span()))
    match = BODY.search(text)
    if match and match.group(1):
        raw = view.raw(*match.span(1))
        # Preserve explicit examination scope verbatim; later aliases cannot
        # silently drop a second anatomical region from the report.
        value = _examination_scope(raw)
        transformations = ({"rule": "explicit_anatomic_scope_separate_from_examination_technique", "raw": raw, "value": value},) if value != raw else ()
        output.append(_candidate(view, "imaging.body_site", {"text": value}, *match.span(1), raw_value=raw, transformations=transformations))
    impression = IMPRESSION.search(text)
    if impression:
        footer = FOOTER.search(text, impression.end())
        end = footer.start() if footer else len(text)
        if end > impression.end():
            raw = view.raw(impression.end(), end)
            output.append(_candidate(view, "imaging.impression", {"text": raw}, impression.end(), end))
    findings = FINDINGS.search(text)
    if findings is None:
        return output
    end = impression.start() if impression and impression.start() > findings.end() else len(text)
    entity_number = 0
    for left, right in _finding_clauses(text[findings.end():end]):
        base = findings.end() + left
        body = text[base:findings.end() + right]
        focal = _positive_focals(body)
        if not focal:
            continue
        measurements = []
        for measure in DIMENSION.finditer(body):
            prefix = body[:measure.start()]
            if ANATOMICAL_SIZE.search(prefix) or not any(m.start() < measure.start() for m in focal):
                continue
            position = _site(prefix, measured=True)
            if position:
                measurements.append((measure, position))
        if not measurements:
            position = _site(body)
            if not position:
                continue
            measurements = [(None, position)]
        previous_end = 0
        previous_site, previous_role, entity = None, None, None
        for measure, (site, transformations) in measurements:
            role, explicit_role = _measurement_role(body[:measure.start()]) if measure else (None, False)
            reuse = (measure is not None and entity is not None and site == previous_site and role != previous_role
                     and explicit_role and not FOCAL.search(body[previous_end:measure.start()]))
            if not reuse:
                entity_number += 1
                entity = f"lesion:{entity_number:03}"
            left = previous_end
            right = measure.end() if measure else len(body)
            # Keep one clause's explicit context with the value; source pieces
            # are literal and never reconstructed by overlap-digit deduplication.
            if not reuse:
                output.append(_candidate(view, "lesion.site", {"text": site}, base + left, base + right,
                                         entity=entity, transformations=transformations))
            side = _laterality(site)
            if side and not reuse:
                output.append(_candidate(view, "lesion.laterality", {"code": side, "raw": site}, base + left, base + right, entity=entity))
            if measure:
                value = _dimension_value(measure.group(), body[:measure.start()])
                value["raw"] = view.raw(base + measure.start(), base + measure.end())
                output.append(_candidate(view, "lesion.dimensions", value, base if reuse else base + left, base + right, entity=entity,
                                         raw_value=value["raw"], limitations=("axes_not_labeled",) if all(c["axis"] is None for c in value["components"]) else ()))
                previous_end = measure.end()
                previous_site, previous_role = site, role
    from .imaging_quantitative import quantitative_candidates

    return output + quantitative_candidates(view, output)


def persist_candidates(report, candidates):
    count = 0
    for order, candidate in enumerate(candidates):
        if not candidate.fragments:
            continue
        raw_text = "\n".join(piece.text for piece in candidate.fragments)
        first = candidate.fragments[0]
        evidence = SourceEvidence.objects.create(
            parsing_version=report.parsing_version, document_page=first.block.document_page,
            ocr_block=first.block if len(candidate.fragments) == 1 else None,
            polygon=first.block.polygon if len(candidate.fragments) == 1 else None,
            source_text=raw_text, confidence=min(piece.block.confidence for piece in candidate.fragments),
        )
        content = field_content(candidate.key, candidate.value, candidate.raw_value,
                                limitations=[*report.limitations, *candidate.limitations], transformations=candidate.transformations)
        fact = Fact(
            document=report.document, document_page=first.block.document_page, parsing_version=report.parsing_version,
            evidence=evidence, origin="AUTOMATIC", category="IMAGING", representation="FIELD", clinical_report=report,
            field_key=candidate.key, entity_key=candidate.entity, schema_version=content["schema_version"],
            raw_text=raw_text, automatic_content=content, reading_order=order, created_by=report.created_by,
        )
        fact.full_clean()
        fact.save()
        for ordinal, piece in enumerate(candidate.fragments):
            fragment_evidence = evidence if len(candidate.fragments) == 1 else SourceEvidence.objects.create(
                parsing_version=report.parsing_version, document_page=piece.block.document_page,
                ocr_block=piece.block, polygon=piece.block.polygon, source_text=piece.text, confidence=piece.block.confidence,
            )
            fragment = FactSourceFragment(
                fact=fact, ordinal=ordinal, document_page=piece.block.document_page, evidence=fragment_evidence,
                ocr_block=piece.block, source_kind="OCR", start_offset=piece.start, end_offset=piece.end,
                raw_text=piece.text, polygon=piece.block.polygon,
            )
            fragment.full_clean()
            fragment.save()
        count += 1
    return count


def extract_clinical_version(version):
    """Trusted processing entry. HTTP callers must first authorize the actor."""
    with transaction.atomic():
        document = version.document
        if not Patient.objects.select_for_update().filter(
            pk=document.patient_id, account__is_active=True, deleted_at__isnull=True,
        ).exists():
            raise PermissionDenied
        document, _ = lock_document_aggregate(document.pk, patient_id=document.patient_id)
        if document.deleted_at is not None:
            raise PermissionDenied
        existing = ClinicalExtraction.objects.filter(parsing_version=version).first()
        if existing:
            return existing
        blocks = list(OcrBlock.objects.filter(parsing_version=version).select_related("document_page").order_by("document_page__page_number", "reading_order", "pk"))
        segments, unparsed = segment_reports(blocks)
        unparsed.update(set(document.pages.values_list("page_number", flat=True)) - {block.document_page.page_number for block in blocks})
        count = 0
        for ordinal, segment in enumerate(segments):
            fingerprint = digest({"version": str(version.pk), "document_sha256": document.sha256,
                                  "pieces": [(str(p.block.pk), p.start, p.end, p.text, p.block.polygon) for p in segment.pieces]})
            report = ClinicalReport(
                document=document, parsing_version=version, origin="AUTOMATIC", ordinal=ordinal, title=segment.title,
                segmenter_version=SEGMENTER_VERSION, schema_version=SCHEMA_VERSION, source_fingerprint=fingerprint,
                lifecycle_revision=document.lifecycle_revision, limitations=segment.limitations,
                boundary_state="LIMITED" if segment.limitations else "CLEAR",
            )
            report.full_clean()
            report.save()
            for span_order, piece in enumerate(segment.pieces):
                span = ClinicalReportSpan(
                    report=report, document_page=piece.block.document_page, ocr_block=piece.block, ordinal=span_order,
                    start_offset=piece.start, end_offset=piece.end, raw_text=piece.text, boundary_basis="EXPLICIT_REPORT_TITLE",
                )
                span.full_clean()
                span.save()
            count += persist_candidates(report, field_candidates(segment))
        return ClinicalExtraction.objects.create(
            parsing_version=version, extractor_version=EXTRACTOR_VERSION, schema_version=SCHEMA_VERSION,
            status="PARTIAL" if segments and (unparsed or any(s.limitations for s in segments)) else "EXTRACTED" if segments else "NO_REPORTS",
            report_count=len(segments), field_count=count, unparsed_page_count=len(unparsed),
            reason="pages_without_report_anchor" if unparsed else "",
            limitations=sorted({reason for segment in segments for reason in segment.limitations}),
        )
