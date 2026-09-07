"""Conservative report-panel context with original OCR fragments, never clock inference."""

from datetime import date, datetime, timezone
import math
import re
import unicodedata


TITLE = re.compile(r'(?:检验|检测|生化)(?:报告单|报告)')
SAMPLE_LABEL = r'(?:采样|采血|采集)(?:日期时间|日期|时间)'
REPORT_LABEL = r'报告(?:日期时间|日期|时间)'
DATE = r'(?P<year>[0-9]{4})[-/.年](?P<month>[0-9]{1,2})[-/.月](?P<day>[0-9]{1,2})日?'
CLOCK = r'(?:T?(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2}))?(?P<utc>Z)?)?'
SPECIMEN = re.compile(r'标本(?:类型|种类|名称)?[:：]?(血清|血浆|全血|末梢血|毛细血管血|中段尿|尿液|尿)')
FASTING = re.compile(r'(?<!非)空腹(?:血糖|葡萄糖)')
REQUEST = re.compile(r'(?:检验|检查|申请)项目[:：]')
FUTURE = re.compile(r'(?:预计|计划|预约|建议|下次|拟于|将于)')


def _compact(text):
    return ''.join(unicodedata.normalize('NFKC', text).split())


def _box(polygon):
    try:
        points = [(float(point[0]), float(point[1])) for point in polygon]
        if len(points) < 3 or not all(math.isfinite(value) and 0 <= value <= 1 for point in points for value in point):
            return None
        xs, ys = zip(*points)
        box = min(xs), min(ys), max(xs), max(ys)
        return box if box[0] < box[2] and box[1] < box[3] else None
    except (ValueError, TypeError, IndexError):
        return None


def _center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _overlap(left, right):
    return max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(0, min(left[3], right[3]) - max(left[1], right[1]))


def _column_bounds(titles, anchor_x):
    """Find horizontal gutters before choosing the preceding report heading.

    Headings in adjacent report columns need not share a top margin. Overlapping
    title footprints form one column; a spanning title that bridges otherwise
    disjoint titles makes the layout ambiguous instead of selecting by height.
    """
    intervals = sorted((item['_box'][0], item['_box'][2]) for item in titles)
    columns = []
    for left, right in intervals:
        if columns and left <= columns[-1][1]:
            columns[-1][1] = max(columns[-1][1], right)
            columns[-1][2].append((left, right))
        else:
            columns.append([left, right, [(left, right)]])
    for _, _, members in columns:
        if any(a[1] < b[0] for a in members for b in members):
            return None
    borders = [0] + [(left[1] + right[0]) / 2 for left, right in zip(columns, columns[1:])] + [1]
    for left, right in zip(borders, borders[1:]):
        if left <= anchor_x <= right:
            return left, right
    return None


def _panel(blocks, anchor_polygon):
    anchor = _box(anchor_polygon)
    if anchor is None:
        return None
    entries = []
    for item in blocks:
        original = _box(item.get('polygon'))
        layout = _box(item.get('layout_polygon') or item.get('polygon'))
        if original is None or layout is None:
            # An unplaced fragment could be a second panel or conflicting date.
            return None
        entries.append({**item, '_box': layout, '_original': original})
    touching = [item for item in entries if _overlap(item['_original'], anchor) > 0]
    if not touching:
        return None
    anchored = max(touching, key=lambda item: _overlap(item['_original'], anchor))
    ax, ay = _center(anchored['_box'])
    titles = [item for item in entries if TITLE.search(_compact(item['text']))]
    if not titles or any(len(TITLE.findall(_compact(item['text']))) > 1 for item in titles):
        return None
    bounds = _column_bounds(titles, ax)
    if bounds is None:
        return None
    left, right = bounds
    titles = [item for item in titles if left <= item['_box'][0] and item['_box'][2] <= right]
    preceding = [item for item in titles if _center(item['_box'])[1] <= ay]
    if not preceding:
        # A cropped upper report is bounded by the next visible report heading.
        heading = min(titles, key=lambda item: item['_box'][1])
        top, bottom = 0, min(item['_box'][1] for item in titles)
    else:
        heading = max(preceding, key=lambda item: _center(item['_box'])[1])
        hy = _center(heading['_box'])[1]
        tolerance = (heading['_box'][3] - heading['_box'][1]) * .6
        if sum(abs(_center(item['_box'])[1] - hy) <= tolerance for item in titles) != 1:
            return None
        top = heading['_box'][1]
        later = [item['_box'][1] for item in titles if _center(item['_box'])[1] > hy + tolerance]
        bottom = min(later, default=1)
    # Without horizontal overlap the visible title could belong to an adjacent
    # report whose partner heading was cropped out. Do not expand it page-wide.
    if min(heading['_box'][2], anchored['_box'][2]) <= max(heading['_box'][0], anchored['_box'][0]):
        return None
    # A block crossing a gutter could contain a conflicting time or two reports.
    if any(top <= _center(item['_box'])[1] < bottom
           and item['_box'][0] < right and item['_box'][2] > left
           and not (left <= item['_box'][0] and item['_box'][2] <= right) for item in entries):
        return None
    selected = [item for item in entries if left <= _center(item['_box'])[0] <= right
                and top <= _center(item['_box'])[1] < bottom]
    return selected if anchored in selected else None


def _text_view(blocks):
    """Read positioned lines; each normalized character retains its raw block and offset."""
    lines = []
    for item in sorted(blocks, key=lambda item: (_center(item['_box'])[1], item['_box'][0])):
        center = _center(item['_box'])[1]
        tolerance = (item['_box'][3] - item['_box'][1]) * .5
        line = next((line for line in reversed(lines) if abs(line[0] - center) < tolerance), None)
        if line is None:
            line = [center, []]
            lines.append(line)
        line[1].append(item)
    text, positions = [], []
    for _, items in lines:
        if text:
            text.append('\n')
            positions.append(None)
        for item in sorted(items, key=lambda item: item['_box'][0]):
            for offset, char in enumerate(item['text']):
                for normalized in unicodedata.normalize('NFKC', char):
                    if normalized in '\r\n':
                        text.append('\n')
                        positions.append((item, offset))
                    elif not normalized.isspace():
                        text.append(normalized)
                        positions.append((item, offset))
    return ''.join(text), positions


def _evidence(positions, start, end):
    spans = []
    for position in positions[start:end]:
        if position is None:
            continue
        item, offset = position
        if spans and spans[-1]['block_id'] == str(item['id']):
            spans[-1]['end'] = max(spans[-1]['end'], offset + 1)
        else:
            spans.append({'block_id': str(item['id']), 'text': item['text'], 'start': offset, 'end': offset + 1,
                          'polygon': item.get('polygon')})
    return spans


def _unknown(reason, evidence=None):
    return {'local': '', 'precision': 'UNKNOWN', 'timezone': '', 'timezone_origin': 'UNCONFIRMED',
            'raw': '', 'raw_label': '', 'role': None, 'utc_datetime': None,
            'reason': reason, 'evidence': evidence or []}


def _time(text, positions, label, role):
    pattern = re.compile(rf'(?P<label>{label})[:：]?{DATE}{CLOCK}(?![0-9])')
    values, evidence, raw_values, labels, invalid = [], [], [], [], False
    for match in pattern.finditer(text):
        if FUTURE.search(text[max(0, match.start() - 8):match.start()].rsplit('\n', 1)[-1]):
            continue
        fragments = _evidence(positions, match.start(), match.end())
        evidence.extend(fragments)
        raw_values.append(' '.join(fragment['text'][fragment['start']:fragment['end']]
                                   for fragment in _evidence(positions, match.start('year'), match.end())))
        labels.append(' '.join(fragment['text'][fragment['start']:fragment['end']]
                               for fragment in _evidence(positions, match.start('label'), match.end('label'))))
        parts = match.groupdict()
        try:
            day = date(int(parts['year']), int(parts['month']), int(parts['day']))
            if parts['hour'] is None:
                local, precision = day.isoformat(), 'DAY'
            else:
                instant = datetime(day.year, day.month, day.day, int(parts['hour']), int(parts['minute']), int(parts['second'] or 0))
                precision = 'SECOND' if parts['second'] is not None else 'MINUTE'
                local = instant.isoformat(timespec='seconds' if precision == 'SECOND' else 'minutes')
            explicit = bool(parts.get('utc'))
            values.append((local, precision, 'UTC' if explicit else '', 'SOURCE_EXPLICIT' if explicit else 'UNCONFIRMED'))
        except ValueError:
            invalid = True
    if invalid:
        return _unknown(f'invalid_{role}_time', evidence)
    unique = set(values)
    if len(unique) != 1:
        return _unknown(f'conflicting_{role}_time' if unique else f'no_{role}_time', evidence)
    local, precision, zone, origin = unique.pop()
    return {'local': local, 'precision': precision, 'timezone': zone, 'timezone_origin': origin,
            'raw': raw_values[0], 'raw_label': labels[0],
            'role': 'SPECIMEN_SAMPLING' if role == 'sample' else 'LAB_REPORT_ISSUANCE',
            'utc_datetime': datetime.fromisoformat(local).replace(tzinfo=timezone.utc).isoformat() if zone == 'UTC' else None,
            'reason': '', 'evidence': evidence}


def report_context(blocks, *, anchor_polygon):
    selected = _panel(blocks, anchor_polygon)
    if selected is None:
        return {'sample_time': _unknown('report_panel_unverified'), 'report_time': _unknown('report_panel_unverified'),
                'time_slot': 'UNSPECIFIED', 'time_slot_evidence': [], 'specimen_raw': '', 'specimen_evidence': [], 'block_ids': []}
    text, positions = _text_view(selected)
    specimens = list(SPECIMEN.finditer(text))
    specimen_values = {match.group(1) for match in specimens}
    specimen_evidence = [fragment for match in specimens for fragment in _evidence(positions, match.start(), match.end())]
    # The request heading can state fasting, but a later instruction cannot.
    anchor = _box(anchor_polygon)
    anchored = max(selected, key=lambda item: _overlap(item['_original'], anchor))
    anchor_y = _center(anchored['_box'])[1]
    fasting = [item for item in selected if _center(item['_box'])[1] < anchor_y
               and (TITLE.search(_compact(item['text'])) or REQUEST.search(_compact(item['text'])))
               and FASTING.search(_compact(item['text'])) and not FUTURE.search(_compact(item['text']))]
    return {'sample_time': _time(text, positions, SAMPLE_LABEL, 'sample'),
            'report_time': _time(text, positions, REPORT_LABEL, 'report'),
            'time_slot': 'FASTING' if fasting else 'UNSPECIFIED',
            'time_slot_evidence': [{'block_id': str(item['id']), 'text': item['text'], 'polygon': item['polygon']} for item in fasting],
            'specimen_raw': specimen_values.pop() if len(specimen_values) == 1 else '',
            'specimen_evidence': specimen_evidence, 'block_ids': [str(item['id']) for item in selected]}
