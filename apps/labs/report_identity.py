"""Original-backed report units, before admission or permanent lab extraction.

Times are local wall-clock readings, not upload timestamps. Their precision is
explicit: a minute reading does not assert that the original contained seconds.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
import re
import unicodedata

from apps.labs.quality import MIN_STANDARD_NAME_CONFIDENCE
from apps.processing.metadata import extract_document_metadata
from apps.processing.models import DocumentType


_SAMPLING = re.compile(r'采样(?:日期|时间)|采集(?:日期|时间)|采血(?:日期|时间)')
_OTHER_LABEL = re.compile(r'报告(?:日期|时间)|打印(?:日期|时间)|接收(?:日期|时间)|接样(?:日期|时间)|签收(?:日期|时间)|送检(?:日期|时间)|审核(?:日期|时间)|检验(?:日期|时间)|姓名|报告号|标本号')
_DATE = re.compile(r'(?<!\d)((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?!\d)')
_CLOCK = re.compile(r'(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])')
_REPORT = re.compile(r'(?:报告(?:单)?号|报告编号|检验单号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)')
_PERSON = re.compile(r'(姓名|患者编号|病历号|门诊号|住院号)\s*[:：]\s*([^\s:：]+)')
_HOSPITAL = re.compile(r'医院|医学中心|检验中心|检测中心|诊所|卫生院')
_HEADING = re.compile(r'检验报告|检验结果|化验报告|血常规')
_PAGE = re.compile(r'第\s*(\d+)\s*页\s*[,，/]?\s*共\s*(\d+)\s*页')


@dataclass(frozen=True)
class ReportUnitEvidence:
    page_number: int
    start_order: int
    end_order: int
    report_number: str = ''
    institution: str = ''
    patient_fields: dict = field(default_factory=dict)
    sampled_at: datetime | None = None
    precision: str = ''
    sampling_dates: tuple = ()
    status: str = 'REJECTED'
    reason: str = 'sampling_datetime_missing'
    identity_reliable: bool = False
    page_index: int | None = None
    page_total: int | None = None
    fields: dict = field(default_factory=dict)
    time_source: str = ''
    source_region: tuple | None = None
    ordinal: int = 0

    @property
    def sampling_label(self):
        if self.sampled_at is None:
            return ''
        return self.sampled_at.isoformat(sep=' ', timespec='seconds' if self.precision == 'SECOND' else 'minutes')

    @property
    def reason_label(self):
        return {
            'sampling_date_missing': '未能识别到完整采样时间：缺少采样日期',
            'sampling_time_missing': '未能识别到完整采样时间：缺少时分',
            'sampling_datetime_missing': '未能识别到完整采样时间',
            'sampling_datetime_unreliable': '采样时间未能可靠识别，请核对原件',
            'sampling_datetime_conflict': '采样时间存在冲突，请核对原件',
            'sampling_source_unavailable': '采样时间来源不可用或关联已撤销，请核对原件',
            'sampling_source_changed': '采样时间来源或关联依据已变化，请重新核对',
            'report_identity_conflict': '报告身份存在冲突，请核对原件',
            'report_revision_conflict': '本次识别依据与既有人工修订不一致，请核对新旧证据',
        }.get(self.reason, '')


def compatible_times(left, left_precision, right, right_precision):
    if left is None or right is None:
        return False
    if left_precision == right_precision == 'SECOND':
        return left == right
    return left.replace(second=0, microsecond=0) == right.replace(second=0, microsecond=0)


def _evidence(page, region, value):
    from apps.processing.geometry import source_polygon

    polygon = source_polygon(page.source_transform, region.polygon)
    return {'value': value, 'raw_text': region.text, 'page_number': page.page_number,
            'reading_order': region.reading_order, 'polygon': polygon,
            'confidence': region.confidence, 'precision': 'region' if polygon else 'page'}


def _sampling(page, regions):
    dates, clocks, complete, evidence = [], [], [], []
    invalid = False
    for region in regions:
        text = unicodedata.normalize('NFKC', region.text)
        labels = list(_SAMPLING.finditer(text))
        for index, label in enumerate(labels):
            part = text[label.end():labels[index + 1].start() if index + 1 < len(labels) else len(text)]
            part = _OTHER_LABEL.split(part, maxsplit=1)[0]
            day_match, clock_match = _DATE.search(part), _CLOCK.search(part)
            day, clock = None, None
            try:
                if day_match:
                    day = date(*(int(value) for value in day_match.groups()))
                if clock_match:
                    hour, minute, second = clock_match.groups()
                    clock = time(int(hour), int(minute), int(second or 0))
            except ValueError:
                invalid = True
                evidence.append(_evidence(page, region, part.strip()))
                continue
            if day:
                dates.append(day)
            if clock:
                clocks.append((clock, 'SECOND' if clock_match.group(3) is not None else 'MINUTE'))
            if day or clock:
                evidence.append(_evidence(page, region, part.strip()))
            if day and clock:
                complete.append((datetime.combine(day, clock), clocks[-1][1]))
            elif not day and not clock:
                invalid = True
    if not complete and len(set(dates)) == 1 and clocks:
        complete = [(datetime.combine(dates[0], clock), precision) for clock, precision in clocks]
    if not complete and dates and clocks:
        return None, '', tuple(sorted(set(dates))), 'REVIEW', 'sampling_datetime_conflict', evidence
    if complete:
        selected = max(complete, key=lambda pair: (pair[1] == 'SECOND', pair[0]))
        conflicting = any(not compatible_times(*selected, *other) for other in complete)
        conflicting |= any(day != selected[0].date() for day in dates)
        conflicting |= any(not compatible_times(*selected, datetime.combine(selected[0].date(), clock), precision)
                           for clock, precision in clocks)
        if conflicting:
            return None, '', tuple(sorted(set(dates))), 'REVIEW', 'sampling_datetime_conflict', evidence
        if invalid or any(item['confidence'] < float(MIN_STANDARD_NAME_CONFIDENCE) for item in evidence):
            return selected[0], selected[1], tuple(sorted(set(dates))), 'REVIEW', 'sampling_datetime_unreliable', evidence
        return selected[0], selected[1], tuple(sorted(set(dates))), 'ACCEPTED', '', evidence
    reason = ('sampling_datetime_unreliable' if invalid else 'sampling_time_missing' if dates
              else 'sampling_date_missing' if clocks else 'sampling_datetime_missing')
    return None, '', tuple(sorted(set(dates))), 'REJECTED', reason, evidence


def _unit(page, regions):
    fields = {'report_number': [], 'institution': [], 'patient': [], 'page': []}
    for region in regions:
        text = unicodedata.normalize('NFKC', region.text).strip()
        for match in _REPORT.finditer(text):
            fields['report_number'].append(_evidence(page, region, match.group(1)))
        if _HOSPITAL.search(text) and 2 <= len(text) <= 96:
            institution = re.sub(r'^(?:医院|检测机构|医疗机构)\s*[:：]\s*', '', text)
            fields['institution'].append(_evidence(page, region, institution))
        for match in _PERSON.finditer(text):
            fields['patient'].append(_evidence(page, region, list(match.groups())))
        match = _PAGE.search(text)
        if match:
            number, total = map(int, match.groups())
            if 1 <= number <= total:
                fields['page'].append(_evidence(page, region, [number, total]))
    sampled, precision, dates, status, reason, times = _sampling(page, regions)
    fields['sampled_at'] = times
    reports = {item['value'] for item in fields['report_number']}
    institutions = {item['value'] for item in fields['institution']}
    patients = {}
    patient_conflict = False
    for item in fields['patient']:
        key, value = item['value']
        patient_conflict |= key in patients and patients[key] != value
        patients[key] = value
    pages = {tuple(item['value']) for item in fields['page']}
    identity_conflict = len(reports) > 1 or len(institutions) > 1 or patient_conflict or len(pages) > 1
    reliable = (len(reports) == len(institutions) == 1 and not identity_conflict
                and all(item['confidence'] >= float(MIN_STANDARD_NAME_CONFIDENCE)
                        for key in ('report_number', 'institution', 'patient', 'page') for item in fields[key]))
    if identity_conflict and status != 'REJECTED':
        status, reason = 'REVIEW', reason or 'report_identity_conflict'
    page_index, page_total = next(iter(pages)) if len(pages) == 1 else (None, None)
    from apps.processing.geometry import source_polygon
    points = [point for region in regions for point in region.polygon]
    x1, x2 = min(point[0] for point in points), max(point[0] for point in points)
    y1, y2 = min(point[1] for point in points), max(point[1] for point in points)
    source_region = source_polygon(page.source_transform, ((x1, y1), (x2, y1), (x2, y2), (x1, y2)))
    return ReportUnitEvidence(page.page_number, regions[0].reading_order, regions[-1].reading_order,
        next(iter(reports)) if len(reports) == 1 else '', next(iter(institutions)) if len(institutions) == 1 else '',
        patients, sampled, precision, dates, status, reason, reliable, page_index, page_total, fields,
        source_region=source_region)


def contains_source_location(region, polygon):
    if not region or not polygon:
        return False
    x = sum(point[0] for point in polygon) / len(polygon)
    y = sum(point[1] for point in polygon) / len(polygon)
    inside = False
    for (ax, ay), (bx, by) in zip(region, (*region[1:], region[0])):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def match_report_unit(row, identities):
    identities = tuple(identities)
    if len(identities) == 1:
        return 0
    polygon = row.field_evidence.get('raw_name', {}).get('polygon') or row.evidence.polygon
    if not polygon:
        return None
    candidates = []
    for index, identity in enumerate(identities):
        if contains_source_location(identity.source_region, polygon):
            candidates.append(index)
    return candidates[0] if len(candidates) == 1 else None


def _table_starts(page, start, end, dictionary):
    """A repeated identity header between actual result tables starts a unit.

    Conflicting fields in one header and report numbers printed in a footer do
    not supply a second table header. Values only establish table locations.
    """
    from .extraction import extract_observations

    regions = page.regions
    markers = {pattern: [index for index in range(start, end)
                        if pattern.search(unicodedata.normalize('NFKC', regions[index].text))]
               for pattern in (_REPORT, _SAMPLING)}
    if not any(len(indices) > 1 for indices in markers.values()):
        return [start]
    spans = []
    for row in extract_observations((replace(page, regions=regions[start:end]),), dictionary):
        if not row.raw_name or not row.raw_value:
            continue
        indices = [index for index in range(start, end)
                   if contains_source_location(row.region, regions[index].polygon)]
        if indices:
            spans.append((min(indices), max(indices)))
    spans.sort()
    starts = [start]
    for previous, current in zip(spans, spans[1:]):
        candidates = [index for indices in markers.values() for index in indices
                      if previous[1] < index < current[0]
                      and any(starts[-1] <= earlier < previous[0] for earlier in indices)]
        if not candidates:
            continue
        boundary = min(candidates)
        hospitals = [index for index in range(previous[1] + 1, boundary)
                     if _HOSPITAL.search(regions[index].text)]
        starts.append(hospitals[-1] if hospitals else boundary)
    return starts


def extract_report_units(pages, *, lab_page_numbers=(), dictionary=None):
    """Split at explicit titles or independent headers separated by result tables.

    The caller may supply pages already classified as laboratory material by an
    existing source-backed classifier. This never borrows metadata across pages.
    """
    units = []
    for page in pages:
        if not page.regions:
            continue
        kind = extract_document_metadata((page,)).document_type
        lab_heading = any(_HEADING.search(region.text) for region in page.regions)
        if (page.page_number not in lab_page_numbers and kind != DocumentType.LAB
                and not (kind in {DocumentType.OTHER, DocumentType.UNKNOWN} and lab_heading)):
            continue
        starts = [0]
        heading_seen = False
        for index, region in enumerate(page.regions):
            if not _HEADING.search(region.text):
                continue
            if heading_seen:
                start = index - 1 if index and _HOSPITAL.search(page.regions[index - 1].text) else index
                if start > starts[-1]:
                    starts.append(start)
            heading_seen = True
        starts = [boundary for start, end in zip(starts, (*starts[1:], len(page.regions)))
                  for boundary in _table_starts(page, start, end, dictionary)]
        for ordinal, (start, end) in enumerate(zip(starts, (*starts[1:], len(page.regions))), 1):
            units.append(replace(_unit(page, page.regions[start:end]), ordinal=ordinal))
    return tuple(units)


def recognize_report_units(pages, dictionary):
    """Use temporary table evidence to classify titleless laboratory screenshots."""
    from .extraction import extract_observations

    titleless = tuple(page for page in pages if extract_document_metadata((page,)).document_type
                      in {DocumentType.UNKNOWN, DocumentType.OTHER})
    lab_pages = {row.page_number for row in extract_observations(titleless, dictionary)}
    return extract_report_units(pages, lab_page_numbers=lab_pages, dictionary=dictionary)


def _same_patient_fields(left, right):
    return all(left.patient_fields[key] == right.patient_fields[key]
               for key in left.patient_fields.keys() & right.patient_fields.keys())


def resolve_continuation_times(items, *, conflicting_pairs=frozenset()):
    """Resolve a single patient's batch only after all units were recognized.

    Keys identify units, not file order. Caller-provided overlap conflicts are
    derived from temporary result evidence, before any permanent admission.
    """
    items = tuple(items)
    output = dict(items)
    for key, unit in items:
        if (unit.status != 'REJECTED' or not unit.identity_reliable or unit.page_index is None
                or unit.page_index <= 1 or unit.reason == 'sampling_datetime_unreliable'):
            continue
        candidates = [(source_key, source) for source_key, source in items if source_key != key
            and source.status == 'ACCEPTED' and source.identity_reliable and source.page_index == 1
            and source.page_total == unit.page_total and source.report_number == unit.report_number
            and source.institution == unit.institution and _same_patient_fields(source, unit)]
        if not candidates or any(frozenset((key, source_key)) in conflicting_pairs for source_key, _ in candidates):
            continue
        source_key, source = max(candidates, key=lambda pair: (pair[1].precision == 'SECOND', str(pair[0])))
        if any(not compatible_times(source.sampled_at, source.precision, other.sampled_at, other.precision)
               for _, other in candidates):
            continue
        if any(day != source.sampled_at.date() for day in unit.sampling_dates):
            continue
        # A clock-only continuation cannot contradict the main report's clock.
        contradictory_clock = False
        for item in unit.fields['sampled_at']:
            match = _CLOCK.search(unicodedata.normalize('NFKC', item['value']))
            if match:
                hour, minute, second = match.groups()
                clock = source.sampled_at.replace(hour=int(hour), minute=int(minute), second=int(second or 0))
                contradictory_clock |= not compatible_times(source.sampled_at, source.precision, clock, 'SECOND' if second else 'MINUTE')
        if contradictory_clock:
            continue
        output[key] = replace(unit, sampled_at=source.sampled_at, precision=source.precision,
                              status='ACCEPTED', reason='', time_source=source_key)
    return output
