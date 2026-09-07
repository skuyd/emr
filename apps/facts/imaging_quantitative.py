"""Additional imaging literals; report-local identities never link examinations."""

import re
import unicodedata

from django.core.exceptions import ValidationError

from .clinical_extraction import (
    DIMENSION, FINDINGS, FOOTER, FOCAL, IMPRESSION, NEGATIVE, SITE, _candidate, _measurement_role, _site,
)
from .clinical_schema import validate_value
from .clinical_segments import _box
from .extraction import explicit_dates


SUV = re.compile(
    r"(?<![A-Za-z])(?:约)?SUV(?:max|最大值)(?:值)?[:：=]?"
    r"(?P<qualifier>约|大约|小于等于|大于等于|小于|大于|不超过|不低于|<=|>=|≤|≥|<|>)?"
    r"(?P<first>\d+(?:\.\d+)?)(?:[~～—–-](?P<second>\d+(?:\.\d+)?))?"
    r"(?P<unit>g/mL|g/ml|无量纲)?(?![\d.A-Za-z])", re.I,
)
BACKGROUND = re.compile(r"背景|本底|血池|参(?:考|照)(?:值|组织|区|本底)?")
ABNORMAL_UPTAKE = re.compile(r"(?:稍|轻度)?增粗|增厚|肿大|异常(?:放射性)?(?:浓聚|摄取)|团块状(?:异常)?浓聚")
COORDINATED_NEGATIVE = re.compile(
    rf"(?:未见|不见|无)(?:明显|明确)?"
    rf"(?:(?:{ABNORMAL_UPTAKE.pattern}|{FOCAL.pattern})(?:以及|及|和|或|与|、))+(?:明显|明确|局部)?$"
)
PRIOR_SUV = re.compile(r"(?:原|前片|上次|既往|此前|前次|先前)(?:的)?(?:检查)?(?:的)?(?:测得|为|约|示|见)*$")
UNKNOWN_SUV_TIME = re.compile(r"(?:日期|时间)(?:角色)?(?:不详|未知|不清)$")
ADRENAL = re.compile(r"[左右双](?:侧)?肾上腺")
COMPARISON = re.compile(r"较前|与前|同前|较上次|与上次|较既往|与既往|对比前片|对比既往|与[^。；]{0,32}(?:比较|对比)")
DATE_LITERAL = r"(?:19|20)\d{2}(?:[-/.年]\d{1,2})?(?:[-/.月]\d{1,2})?[年月日]?"
REFERENCE = re.compile(
    rf"(?:对比|比较)(?:前片|既往(?:检查)?|上次(?:检查)?)[（(][^()（）:：]{{1,40}}(?::\d{{2}})?[）)]"
    rf"|与{DATE_LITERAL}(?:PET/CT|CT|MR|MRI|超声|检查|前片)?(?:比较|对比)"
    rf"|(?:对比|比较){DATE_LITERAL}(?:PET/CT|CT|MR|MRI|超声|检查|前片)?", re.I,
)
GROUP_LARGER = re.compile(r"较大者|较大的|大者|较大(?=短径|长径|直径|病灶|结节)")
REPORT_MAXIMUM = re.compile(r"(?:本次(?:检查)?|本报告|全报告|所有病灶中)(?:的|所见|中|为)?最大(?:病灶|结节|肿块)")
UNCERTAIN_MAXIMUM = re.compile(r"不是|并非|未能|不能|是否|不确定|无法|可能")
REFERENCE_LIMIT = "comparison_is_literal_not_linked_examination"
COMPLETE_COMPARISON = re.compile(r"(?:同前|较前(?:次)?(?:无明显变化|未见明显变化|相仿|稍缩小|缩小|增大|增多|减少|改善))[。]?$|无明显变化[。]?$")


def _normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _site_before(text):
    position = _site(text, measured=True)
    last_adrenal = list(ADRENAL.finditer(text))
    sites = list(SITE.finditer(text))
    if last_adrenal and (not sites or last_adrenal[-1].end() >= sites[-1].end()):
        return last_adrenal[-1].group(), ()
    return position


def _anchor(candidate, view):
    own = view.text[candidate.start:candidate.end]
    site = _normalized(candidate.value["text"])
    at = own.rfind(site)
    if at >= 0:
        return candidate.start + at
    # The original field can explicitly compose an organ with a later segment.
    # Use that original candidate's final named location only inside its span.
    matches = list(SITE.finditer(own))
    return candidate.start + matches[-1].start() if matches else candidate.start


def _matching_site(sites, view, start, end, point, literal):
    matches = [candidate for candidate in sites
               if candidate.start < end and candidate.end > start
               and _normalized(candidate.value["text"]) == _normalized(literal)
               and _anchor(candidate, view) <= point]
    return max(matches, key=lambda candidate: _anchor(candidate, view)) if matches else None


def _scalar(match, view, base, prefix):
    qualifier = match.group("qualifier")
    comparator = {"<": "LT", "小于": "LT", "<=": "LE", "≤": "LE", "小于等于": "LE", "不超过": "LE",
                  ">": "GT", "大于": "GT", ">=": "GE", "≥": "GE", "大于等于": "GE", "不低于": "GE"}.get(qualifier, "EQ")
    numbers = [match.group("first")]
    if match.group("second"):
        if comparator != "EQ":
            return None  # Conflicting notation remains in the untouched report.
        comparator = "RANGE"
        numbers.append(match.group("second"))
    temporal_prefix = prefix.rstrip("，,:：")
    role = _measurement_role(temporal_prefix)[0]
    if UNKNOWN_SUV_TIME.search(temporal_prefix):
        role = "UNKNOWN"
    elif PRIOR_SUV.search(temporal_prefix):
        role = "HISTORICAL"
    raw = view.raw(base + match.start(), base + match.end())
    value = dict(values=numbers, comparator=comparator, unit=match.group("unit"),
                 approximate=qualifier in {"约", "大约"} or match.group().startswith("约"),
                 measurement_role=role, raw=raw)
    try:
        validate_value("lesion.suvmax", value)
    except ValidationError:
        return None
    return value


def _uptake_candidates(view, start, end, existing):
    result, sites = [], [candidate for candidate in existing if candidate.key == "lesion.site"]
    for clause in re.finditer(r"[^。；]+[。；]?", view.text[start:end]):
        body, base = clause.group(), start + clause.start()
        for match in SUV.finditer(body):
            prefix = body[:match.start()]
            position = _site_before(prefix)
            if not position:
                continue
            literal, transformations = position
            chosen = _matching_site(sites, view, base, base + len(body), base + match.start(), literal)
            locations = list(SITE.finditer(prefix))
            local_start = locations[-1].start() if locations else 0
            following = [candidate for candidate in sites if candidate.start < base + len(body)
                         and _anchor(candidate, view) > base + match.end()]
            right = min((_anchor(candidate, view) - base for candidate in following), default=len(body))
            if BACKGROUND.search(body[local_start:right]):
                continue
            if chosen is None:
                local = prefix[local_start:]
                # An explicit negative also governs its coordinated list. It
                # stops at punctuation or a new assertion/contrast; do not
                # reuse a negative from another sentence to suppress a finding.
                def negated(marker):
                    before = local[:marker.start()]
                    return NEGATIVE.search(before) or COORDINATED_NEGATIVE.search(before)

                focal = any(not negated(marker) for marker in FOCAL.finditer(local))
                abnormal = any(not negated(marker) for marker in ABNORMAL_UPTAKE.finditer(local))
                if not focal and not abnormal:
                    continue
                # An explicit local abnormality can have uptake without a size.
                # It remains an unconfirmed report observation, not a tumor.
                entity = f"lesion:uptake-{len([s for s in sites if s.entity.startswith('lesion:uptake-')]) + 1:03}"
                chosen = _candidate(view, "lesion.site", {"text": literal}, base + local_start, base + right,
                                    entity=entity, transformations=transformations,
                                    limitations=("reported_local_abnormality_not_tumor_classification",))
                sites.append(chosen)
                result.append(chosen)
            value = _scalar(match, view, base, prefix)
            if value is None:
                continue
            left = max(base, chosen.start)
            # Keep the named site and qualifying language with each uptake.
            # Distinct subsequent lesions are outside this field's fragment.
            result.append(_candidate(view, "lesion.suvmax", value, left, base + right,
                                     entity=chosen.entity, raw_value=value["raw"],
                                     limitations=("suv_is_source_reported_not_cross_method_comparable",)))
    return result


def _maximum_candidates(view, start, end, existing, *, named_reference=False):
    result = []
    sites = [candidate for candidate in existing if candidate.key == "lesion.site"]
    previous = None
    for clause in re.finditer(r"[^。；]+[。；]?", view.text[start:end]):
        body, base = clause.group(), start + clause.start()
        markers = sorted([(match, "GROUP_LARGER") for match in GROUP_LARGER.finditer(body)]
                         + [(match, "REPORT_MAXIMUM") for match in REPORT_MAXIMUM.finditer(body)], key=lambda pair: pair[0].start())
        for marker, code in markers:
            if UNCERTAIN_MAXIMUM.search(body[max(0, marker.start() - 20):marker.end() + 12]):
                continue
            choices = [candidate for candidate in sites if candidate.start < base + len(body) and candidate.end > base]
            if named_reference:
                position = _site_before(body)
                choices = [candidate for candidate in sites if position and _normalized(candidate.value["text"]) == _normalized(position[0])]
            # A single following "larger short diameter" sentence can refer to
            # one explicitly named group in the immediately preceding sentence.
            if not choices and previous and marker.start() == 0 and code == "GROUP_LARGER":
                choices = previous[1]
            if len({candidate.entity for candidate in choices}) != 1:
                continue
            chosen = choices[0]
            evidence_end = base + len(body)
            if not named_reference:
                measurement = DIMENSION.search(body, marker.end())
                # The literal larger qualifier is evidenced by its named
                # observation and measurement; unrelated later descriptions
                # are not required for this field's source fragment.
                evidence_end = base + (measurement.end() if measurement else marker.end())
            result.append(_candidate(view, "lesion.maximum_scope", {"code": code, "raw": view.raw(base + marker.start(), base + marker.end())},
                                     base if named_reference else min(chosen.start, base), evidence_end, entity=chosen.entity,
                                     limitations=("maximum_scope_is_explicit_report_wording",)))
        previous = (body, [candidate for candidate in sites if candidate.start < base + len(body) and candidate.end > base])
    return result


def _complete_comparison_line_ends(view, start, end):
    cuts = []
    for index in range(start + 1, end):
        before_piece, before_offset = view.offsets[index - 1]
        after_piece, after_offset = view.offsets[index]
        before, after = view.pieces[before_piece].block, view.pieces[after_piece].block
        if before.pk == after.pk:
            line_end = "\n" in before.text[before_offset + 1:after_offset]
        else:
            a, b = _box(before), _box(after)
            line_end = "\n" in before.text[before_offset + 1:]
            if a and b:
                line_end = line_end or abs((a[1] + a[3]) - (b[1] + b[3])) > max(a[3] - a[1], b[3] - b[1])
        if (line_end and COMPLETE_COMPARISON.search(view.text[max(start, index - 24):index])
                and not re.match(r"但|并|且|伴|及|与|[、，,；;（(]", view.text[index:])):
            cuts.append(index)
    return cuts


def _comparison_candidates(view, start, end):
    result, covered = [], []

    def statement(left, right, *, reference=False):
        raw = view.raw(left, right)
        entity = f"comparison:{len([row for row in result if row.key == 'comparison.statement']) + 1:03}"
        result.append(_candidate(view, "comparison.statement", {"text": raw}, left, right,
                                 entity=entity, limitations=(REFERENCE_LIMIT,)))
        if reference:
            dates = explicit_dates(view.text[left:right])
            if not dates and re.search(r"(?:日期|时间)(?:不详|未知|不清)", view.text[left:right]):
                dates = [{"value": None, "precision": "UNKNOWN"}]
            for date in dates:
                value = {"value": date["value"], "precision": date["precision"]}
                result.append(_candidate(view, "comparison.reference_date", value, left, right,
                                         entity=entity, limitations=(REFERENCE_LIMIT,)))

    for match in REFERENCE.finditer(view.text, start, end):
        statement(*match.span(), reference=True)
        covered.append(match.span())
    # Explicit section labels and numbered items delimit statements. Ordinary
    # line wrapping and semicolons do not split a compound source assertion.
    section = view.text[start:end]
    boundaries = sorted({0, len(section), *[m.start() for m in IMPRESSION.finditer(section)],
                         *[index - start for index in _complete_comparison_line_ends(view, start, end)]})
    for left, right in zip(boundaries, boundaries[1:]):
        label = IMPRESSION.match(section, left)
        if label:
            left = label.end()
        for sentence in re.finditer(r"[^。]+(?:。|$)", section[left:right]):
            begin = start + left + sentence.start()
            finish = start + left + sentence.end()
            header = next(((a, b) for a, b in covered if begin <= a < finish), None)
            if header:
                begin = header[1]
                while begin < finish and view.text[begin] in ":：":
                    begin += 1
            text = view.text[begin:finish]
            enumeration = re.match(r"\d+[、.．]", text)
            if enumeration:
                begin += enumeration.end()
                text = view.text[begin:finish]
            if text and COMPARISON.search(text):
                statement(begin, finish)
    return result


def quantitative_candidates(view, existing):
    findings = FINDINGS.search(view.text)
    if findings is None:
        return []
    impression = IMPRESSION.search(view.text, findings.end())
    footer = FOOTER.search(view.text, findings.end())
    report_end = footer.start() if footer else len(view.text)
    findings_end = impression.start() if impression else report_end
    uptake = _uptake_candidates(view, findings.end(), findings_end, existing)
    impression_maxima = _maximum_candidates(view, impression.end(), report_end, [*existing, *uptake], named_reference=True) if impression else []
    return [*uptake, *_maximum_candidates(view, findings.end(), findings_end, [*existing, *uptake]), *impression_maxima,
            *_comparison_candidates(view, findings.end(), report_end)]
