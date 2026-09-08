"""Finite headed source slots with an original character map, without writes."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from itertools import groupby
import re

from apps.processing.value_objects import InvalidRegion, normalized_polygon

from .matching import _view


LAYOUT_VERSION = 'reported-cancer-narrative-layout-2'
_ROOTS = {'首次病程记录': 'ADMISSION', '会诊记录': 'CONSULTATION', '会诊意见': 'CONSULTATION',
          '病例特点': 'CHARACTERISTICS'}
_NEW_RECORD = {'首次病程记录', '会诊记录'}
_ROLES = {'主诉': 'CHIEF_COMPLAINT', '现病史': 'PRESENT_ILLNESS', '辅助检查': 'AUXILIARY_FINDINGS',
          '病史': 'PRESENT_ILLNESS', '病史摘要': 'CONSULTATION_SUMMARY'}
_EXCLUDED = {'质控', '质控说明', '质量控制', '参考文献', '文献', '图谱', '说明', '指南案例',
             '送检者提供诊断', '送检资料', '临床用药', '治疗适应证'}
_STOP = {'查体', '体格检查', '诊断', '入院诊断', '出院诊断', '初步诊断', '病理诊断',
         '诊疗计划', '诊治经过', '治疗经过', '治疗方案', '建议', '鉴别诊断', '个人史', '家族史',
         '既往史', '婚育史', '签名', '医师签名', '医生签名', '记录人', '姓名', '性别', '年龄',
         '患者信息', '标本', '病理报告', '页码'}
_DATES = {'记录日期', '记录时间'}
_NAMES = sorted(set(_ROOTS) | set(_ROLES) | _EXCLUDED | _STOP | _DATES, key=len, reverse=True)
_HEADER = re.compile(r'^(?:\d+[.、)]|[一二三四五六七八九十]+、)?[【\[]?('
                     + '|'.join(map(re.escape, _NAMES)) + r')[】\]]?')
_NAMED_BOUNDARY = (r'(?:\d+[.、)]|[一二三四五六七八九十]+、)?[【\[]?'
                   r'[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z\s]{0,24}[】\]]?\s*[:：]')
_INLINE = re.compile(r'(?<=[。！？!?；;\s])(?=' + _NAMED_BOUNDARY + r')')
_DATE = re.compile(r'(?<!\d)(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})日?(?!\d)')
_UNKNOWN_BOUNDARY = re.compile(r'^\s*(?:' + _NAMED_BOUNDARY
                               + r'|[\u3400-\u9fffA-Za-z]{1,30}(?:记录|报告|病历)\s*$)')
_SENTENCE = re.compile(r'[^。！？!?；;]+(?:[。！？!?；;]+|$)')


def source_fragments(positions):
    """Join contiguous original characters only; synthesized gaps have no source."""
    result = []
    for point in positions:
        if point is None:
            continue
        block, offset = point
        if result and result[-1]['block_id'] == str(block.pk) and result[-1]['end'] == offset:
            result[-1]['end'] += 1
            result[-1]['raw'] = block.text[result[-1]['start']:offset + 1]
        else:
            result.append({'block_id': str(block.pk), 'page_id': str(block.document_page_id),
                'version_id': str(block.parsing_version_id), 'reading_order': block.reading_order,
                'start': offset, 'end': offset + 1, 'raw': block.text[offset:offset + 1],
                'polygon': deepcopy(block.polygon),
                'confidence': str(block.confidence) if block.confidence is not None else None})
    return tuple(result)


@dataclass(frozen=True)
class NarrativeInput:
    page_id: str
    version_id: str
    role: str
    text: str
    positions: tuple
    body_start: int
    heading_fragments: tuple
    record_dates: tuple = ()

    def __post_init__(self):
        if (len(self.text) != len(self.positions) or not 0 <= self.body_start < len(self.text)
                or self.role not in {*_ROLES.values(), 'ADMISSION_NARRATIVE'} or not self.heading_fragments):
            raise ValueError('叙述原文及范围不完整。')
        for character, point in zip(self.text, self.positions):
            if point is None:
                if not character.isspace():
                    raise ValueError('原文字符缺少实际位置。')
                continue
            block, offset = point
            if (str(block.document_page_id) != self.page_id or str(block.parsing_version_id) != self.version_id):
                raise ValueError('叙述位置必须属于同一原页和解析版本。')
            if type(offset) is not int or not 0 <= offset < len(block.text) or block.text[offset] != character:
                raise ValueError('叙述字符不等于原文切片。')

    @property
    def body(self):
        return self.text[self.body_start:]

    def fragments(self, start, end):
        if not 0 <= start <= end <= len(self.text):
            raise ValueError('叙述区间超出原文。')
        return source_fragments(self.positions[start:end])

    def candidate_binding(self, row):
        if (self.text[row['start']:row['end']] != row['raw'] or
                self.text[row['match_start']:row['match_end']] != row['label_raw']):
            raise ValueError('叙述候选与原字符区间不符。')
        section = self.fragments(0, len(self.text))
        return {'binding_kind': 'NARRATIVE_OCR', 'role': self.role,
                'fragments': list(self.fragments(row['start'], row['end'])),
                'label_fragments': list(self.fragments(row['match_start'], row['match_end'])),
                'heading_fragments': list(self.heading_fragments), 'section_fragments': list(section),
                'date_fragments': [fragment for item in self.record_dates for fragment in item['fragments']],
                'confidence_values': tuple(item['confidence'] for item in section)}


@dataclass(frozen=True)
class NarrativeDiscovery:
    inputs: tuple
    coverage: tuple
    complete: bool
    relevant: bool


@dataclass(frozen=True)
class _Line:
    text: str
    positions: tuple
    box: tuple

    def part(self, start=0, end=None):
        return _Line(self.text[start:end], self.positions[start:end], self.box)


def _join(parts):
    text, positions, ranges = '', [], []
    for part in parts:
        if not part.text:
            ranges.append((len(text), len(text)))
            continue
        separator, mapping = '', []
        if text and not (text[-1].isspace() or part.text[0].isspace()):
            previous, following = positions[-1], part.positions[0]
            if previous is not None and following is not None and previous[0].pk == following[0].pk:
                block, start = previous[0], previous[1] + 1
                end = following[1]
                if start <= end and not block.text[start:end].strip():
                    separator = block.text[start:end]
                    mapping = [(block, offset) for offset in range(start, end)]
                else:
                    separator, mapping = '\n', [None]
            else:
                separator, mapping = '\n', [None]
        text += separator
        positions.extend(mapping)
        start = len(text)
        text += part.text
        positions.extend(part.positions)
        ranges.append((start, len(text)))
    return text, tuple(positions), ranges


def _heading(line):
    text, positions = _view(line.text)
    match = _HEADER.match(text)
    if not match:
        return None
    end = match.end()
    if end < len(text) and text[end] == ':':
        return match.group(1), positions[end] + 1
    if end == len(text):
        return match.group(1), len(line.text)
    gap = line.text[positions[end - 1] + 1:positions[end]]
    if gap and gap.isspace():
        return match.group(1), positions[end]
    return None


def _lines(blocks):
    """Use geometry for adjacent single-line boxes, never search joined text."""
    single, multiple, invalid = [], [], False
    for block in blocks:
        try:
            normalized_polygon(block.polygon)
            points = normalized_polygon(getattr(block, 'layout_polygon', None) or block.polygon)
        except (InvalidRegion, TypeError):
            invalid = True
            continue
        xs, ys = zip(*points)
        box = (min(xs), min(ys), max(xs), max(ys))
        chunks = list(re.finditer(r'[^\r\n]+', block.text))
        for index, chunk in enumerate(chunks):
            line = _Line(chunk.group(), tuple((block, i) for i in range(*chunk.span())), box)
            if len(chunks) == 1:
                single.append(line)
            else:
                # A multiline provider box gives original offsets/order, not
                # invented per-line source polygons or adjacent-column evidence.
                multiple.append((box[1], box[0], block.reading_order, index, line))
    rows = []
    for line in sorted(single, key=lambda item: ((item.box[1] + item.box[3]) / 2, item.box[0])):
        row = next((items for items in reversed(rows[-5:]) if min(items[0].box[3], line.box[3])
                    - max(items[0].box[1], line.box[1]) >= .6 * min(
                        items[0].box[3] - items[0].box[1], line.box[3] - line.box[1])), None)
        if row is None:
            rows.append([line])
        else:
            row.append(line)
    ready = list(multiple)
    for row in rows:
        pending = None
        for line in sorted(row, key=lambda item: item.box[0]):
            if pending:
                width = max((pending.box[2] - pending.box[0]) / max(len(pending.text), 1),
                            (line.box[2] - line.box[0]) / max(len(line.text), 1))
                if -.1 * width <= line.box[0] - pending.box[2] <= 1.6 * width:
                    text, positions, _ = _join([pending, line])
                    pending = _Line(text, positions, (pending.box[0], min(pending.box[1], line.box[1]),
                                                       line.box[2], max(pending.box[3], line.box[3])))
                    continue
                ready.append((*pending.box[1::-1], pending.positions[0][0].reading_order, 0, pending))
            pending = line
        if pending:
            ready.append((*pending.box[1::-1], pending.positions[0][0].reading_order, 0, pending))
    result = []
    for *_, line in sorted(ready, key=lambda item: item[:4]):
        cuts = [0] + [match.start() for match in _INLINE.finditer(line.text)] + [len(line.text)]
        result.extend(line.part(start, end) for start, end in zip(cuts, cuts[1:]) if end > start)
    return result, invalid


def _adjacent(previous, current):
    left, right = previous.positions[-1], current.positions[0]
    if left[0].pk == right[0].pk:
        return left[1] < right[1]
    # A separate horizontal field is not a continuation, even when OCR assigns
    # it the next reading_order. New explicit headings are handled separately.
    return (current.box[1] >= previous.box[3] - .005
            and current.box[1] - previous.box[3] <= max(.08, 4 * (previous.box[3] - previous.box[1]))
            and abs(current.box[0] - previous.box[0]) <= .08)


def _record_dates(parts):
    result = []
    for piece, kind in parts:
        if kind != 'DATE':
            continue
        for match in _DATE.finditer(piece.text):
            try:
                value = date(*(int(item) for item in match.groups())).isoformat()
            except ValueError:
                value = None
            result.append({'value': value, 'fragments': list(source_fragments(piece.positions))})
    return tuple(result)


def _admission_bodies(body):
    """Locate each introduction sentence without granting its role to neighbours.

    OCR line wrapping can remain inside a sentence. Keep the complete original
    sentence (including subject/negation) and its existing character map, rather
    than cutting a clean substring at 因 and losing governing context.
    """
    text, _, ranges = _join(body)
    selected, unproved = [], False
    for sentence in _SENTENCE.finditer(text):
        if not sentence.group().strip():
            continue
        if not re.search(r'因.+?(?:收入院|入院)', sentence.group(), re.DOTALL):
            unproved = True
            continue
        start, end = sentence.span()
        selected.append([part.part(max(start, left) - left, min(end, right) - left)
                         for part, (left, right) in zip(body, ranges)
                         if max(start, left) < min(end, right)])
    return selected, unproved


def _page_inputs(page_blocks):
    lines, invalid = _lines(page_blocks)
    inputs, reasons, current, context = [], set(), None, []
    root, excluded, relevant, previous = '', False, False, None
    if invalid:
        reasons.add('invalid_geometry')

    def finish():
        nonlocal current
        if current is None:
            return
        prefix, body, role = current['prefix'], current['body'], current['role']
        if not body or not any(line.text.strip() for line in body):
            reasons.add('empty_heading_body')
            current = None
            return
        bodies = [body]
        if role == 'ADMISSION_NARRATIVE':
            bodies, unproved = _admission_bodies(body)
            if unproved or not bodies:
                reasons.add('unproved_admission_slot')
        for selected_body in bodies:
            pieces = [line for line, _ in prefix] + selected_body
            text, positions, ranges = _join(pieces)
            body_start = ranges[len(prefix)][0]
            heading = tuple(fragment for (start, end), (_, kind) in zip(ranges, prefix)
                            if kind == 'HEADING' for fragment in source_fragments(positions[start:end]))
            origin = next(point[0] for part in selected_body for point in part.positions if point is not None)
            inputs.append(NarrativeInput(str(origin.document_page_id), str(origin.parsing_version_id), role,
                text, positions, body_start, heading, _record_dates(prefix)))
        current = None

    for original_line in lines:
        line = original_line
        if previous and not _adjacent(previous, line):
            if current:
                reasons.add('discontinuous_scope')
            finish()
            context, root = [], ''
        previous = original_line
        while line.text.strip():
            found = _heading(line)
            if found:
                name, offset = found
                head, remainder = line.part(0, offset), line.part(offset)
                if name in _ROOTS:
                    if excluded and name not in _NEW_RECORD:
                        break
                    finish()
                    root, excluded, context = _ROOTS[name], False, [(head, 'HEADING')]
                    relevant = True
                    line = remainder
                    continue
                if name in _EXCLUDED:
                    finish()
                    excluded, root, context = True, '', []
                    break
                if excluded:
                    break
                if name in _DATES:
                    finish()
                    if root:
                        context.append((line, 'DATE'))
                    break
                if name in _STOP:
                    finish()
                    root, context = '', []
                    break
                if name in _ROLES:
                    finish()
                    if (name == '病史' and root != 'CHARACTERISTICS') or (name == '病史摘要' and root != 'CONSULTATION'):
                        reasons.add('missing_parent_heading')
                        break
                    relevant = True
                    current = {'prefix': context + [(head, 'HEADING')], 'role': _ROLES[name],
                               'body': [remainder] if remainder.text.strip() else []}
                    break
            if excluded:
                break
            if _UNKNOWN_BOUNDARY.match(line.text):
                finish()
                context, root = [], ''
                reasons.add('unsupported_section_boundary')
                break
            if current:
                current['body'].append(line)
            elif root == 'ADMISSION':
                current = {'prefix': list(context), 'role': 'ADMISSION_NARRATIVE', 'body': [line]}
            elif root == 'CONSULTATION' and re.match(r'\s*(?:患者|本患者).*(?:诊断|确诊)', line.text):
                current = {'prefix': list(context), 'role': 'CONSULTATION_SUMMARY', 'body': [line]}
            break
    finish()
    status = 'UNJUDGED' if reasons else 'SCOPED' if inputs else 'OUT_OF_SCOPE' if excluded else 'UNJUDGED'
    if status == 'UNJUDGED' and not reasons:
        reasons.add('no_supported_heading')
    return inputs, {'page_id': str(page_blocks[0].document_page_id), 'status': status,
                    'reasons': sorted(reasons), 'slot_count': len(inputs), 'relevant': relevant}, not reasons or reasons == {'no_supported_heading'}


def discover_narratives(blocks):
    """Discover bounded source roles before looking for any cancer literal."""
    blocks = tuple(blocks)
    if len({str(block.parsing_version_id) for block in blocks}) > 1:
        raise ValueError('叙述发现只能使用同一解析版本。')
    inputs, coverage, complete = [], [], True
    for _, group in groupby(sorted(blocks, key=lambda item: (str(item.document_page_id), item.reading_order, str(item.pk))),
                            key=lambda item: str(item.document_page_id)):
        found, page, page_complete = _page_inputs(list(group))
        inputs.extend(found)
        coverage.append(page)
        complete = complete and page_complete
    return NarrativeDiscovery(tuple(inputs), tuple(coverage), complete, any(row['relevant'] for row in coverage))
