"""Associate OCR cells using observed headers; never compact away empty columns."""

from dataclasses import dataclass, field
import re
import unicodedata

from .candidates import _bounds, _rows, _name_regions_and_text


LAYOUT_RULE_VERSION = 'lab-layout-v3'
HEADERS = {
    'raw_name': {'项目', '项目名称', '检验项目', '检测项目', '名称', 'item', 'test'},
    'raw_value': {'结果', '检验结果', '检测结果', '测定值', 'result'},
    'raw_unit': {'单位', '标志单位', 'unit', 'units'},
    'reference_range_raw': {'参考值', '参考范围', '正常范围', '参考区间', 'reference', 'range'},
    'report_flag_raw': {'提示', '标志', '标记', '异常提示', 'flag'},
    'method_raw': {'方法', '检测方法', '测试方法', '测定方法', 'method'},
    'row_number': {'序', '序号', '编号'},
    'project_code': {'代号', '项目代号', '项目代码', '英文名称', '缩写'},
    'row_code': {'序代号', '序号代号'},
    'recognition_mark': {'互认标识'},
}
SPECIMENS = {'全血': 'BLOOD', '血液': 'BLOOD', '血清': 'BLOOD', '血浆': 'BLOOD', '尿液': 'URINE', '尿': 'URINE', '粪便': 'STOOL', '大便': 'STOOL'}
PANELS = {'血常规': 'CBC', '尿常规': 'URINALYSIS', '凝血功能': 'COAGULATION', '血凝': 'COAGULATION', '炎症指标': 'INFLAMMATION'}
_NON_LAB_SECTION = re.compile(
    r'^(?:[\d.、)]+)?(?:备注|注释|说明|结果说明|检测说明|参考文献|附录|基因列表|'
    r'基因变异(?:总览|结果总览|结果详细解析)):?$|'
    r'^.{0,32}?(?:基因检测报告|病理(?:诊断)?报告|(?:超声|影像|放射|CT|MRI)(?:检查)?报告)'
    r'(?:单|书)?$|^(?:入院|出院|病程|治疗|放疗|化疗)记录$|^(?:长期|临时)?医嘱(?:单)?$',
    re.I,
)


def _non_lab_section(row):
    # Match a section title, not a keyword mentioned in a laboratory result.
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', ''.join(x.text for x in row)))
    return bool(_NON_LAB_SECTION.fullmatch(compact))


def _fragmented_without_cells(row, dictionary, scope_cache):
    # Glyphs in prose/references are not item/result cells. In particular, a K/P
    # inside a word must not start a new electrolyte row. A complete approved
    # label or unit cell can establish row structure without requiring a mapping.
    glyphs = sum(len(region.text.strip()) == 1 for region in row)
    if glyphs < 6 or glyphs <= len(row) / 2:
        return False
    from .dictionary import default_dictionary, normalize_indicator_alias
    from .extraction import _unit_key, _result_type
    from tools.sample_dictionary.normalize import normalize_candidate_name

    # Most reports already have intact cells. Build this once per extraction,
    # only when glyph fragments actually need the extra scope evidence.
    if not scope_cache:
        scope_dictionary = dictionary or default_dictionary()
        scope_cache['names'] = {normalize_indicator_alias(name) for item in scope_dictionary.indicators
                               for name in (item.standard_name, *item.aliases, *item.ocr_variants)}
        scope_cache['units'] = {_unit_key(unit) for item in scope_dictionary.indicators for unit in item.unit_forms}
    names, units = scope_cache['names'], scope_cache['units']
    name_regions, raw_name = _name_regions_and_text(row)
    name = normalize_indicator_alias(normalize_candidate_name(raw_name))
    if len(name) > 1 and name in names:
        return False
    tail = row[len(name_regions):]
    if (re.fullmatch(r'[\u3400-\u9fff]{2,}', name) and 1 <= len(tail) <= 2
            and _result_type(tail[0].text)
            and (len(tail) == 1 or _unit_key(tail[1].text) in units)):
        # Candidate collection does not require a published dictionary mapping.
        # A split Chinese label with intact result/unit cells is still reviewable.
        return False
    return not any(len(region.text.strip()) > 1 and _unit_key(region.text) in units for region in row)


def _update_templates(templates, observed, ended_tables, outside_lab):
    accepted = []
    rejected = set()
    for left, right, columns in observed:
        name_x = next(x for x, role in columns if role == 'raw_name')
        ended = outside_lab or any(start <= name_x < end for start, end in ended_tables)
        if ended and not {'raw_unit', 'reference_range_raw'} & {role for _, role in columns}:
            rejected.add((left, right))
            continue
        accepted.append((left, right, columns))
    if not accepted:
        return templates, ended_tables, outside_lab, set()
    # A repeated header within one existing column updates only that table.
    # It must not broaden to the page width or reopen an adjacent ended section.
    updates = {}
    for start, end, columns in observed:
        local = [(left, right) for left, right, _ in templates
                 if all(left <= x < right for x, _ in columns)]
        if len(local) != 1 or local[0] in updates:
            return tuple(observed), rejected, False, {(left, right) for left, right, _ in observed}
        updates[local[0]] = (columns, (start, end) in rejected)
    merged = tuple((left, right, updates.get((left, right), (columns, False))[0]) for left, right, columns in templates)
    ended = (ended_tables - updates.keys()) | {key for key, (_, blocked) in updates.items() if blocked}
    return merged, ended, False, set(updates)


def quality_issue(code, fields, details):
    return {'code': code, 'rule_version': LAYOUT_RULE_VERSION, 'fields': list(fields), 'details': details}


@dataclass
class AssociatedRow:
    page: object
    regions: tuple
    fields: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    specimen: str = ''
    panel: str = ''
    specimen_source: tuple = ()


def _header(region):
    token = re.sub(r'\s+', '', unicodedata.normalize('NFKC', region.text)).casefold().strip(':：')
    return next((name for name, forms in HEADERS.items() if token in forms), None)


def _templates(row):
    recognized = [(region, _header(region)) for region in row if _header(region)]
    groups, current = [], []
    for position, (region, role) in enumerate(recognized):
        roles = {item[1] for item in current}
        # Serial/code headers may be merged on one table and split on the
        # other. They start the next table before its name header repeats.
        remaining = {other_role for _, other_role in recognized[position + 1:]}
        starts_table = (role in {'row_number', 'project_code', 'row_code'}
                        and {'raw_name', 'raw_value'} <= roles & remaining)
        if role in roles or starts_table:
            groups.append(current)
            current = []
        current.append((region, role))
    if current:
        groups.append(current)
    if not groups or any(not {'raw_name', 'raw_value'} <= {role for _, role in group} for group in groups):
        return ()
    templates = []
    for index, group in enumerate(groups):
        left = 0 if index == 0 else (_bounds(groups[index - 1][-1][0])[2] + _bounds(group[0][0])[0]) / 2
        right = 1 if index == len(groups) - 1 else (_bounds(group[-1][0])[2] + _bounds(groups[index + 1][0][0])[0]) / 2
        templates.append((left, right, tuple((_bounds(region)[0], role) for region, role in group)))
    return tuple(templates)


def _column_role(region, columns):
    x, _top, right, _bottom = _bounds(region)
    role = min(columns, key=lambda item: abs(item[0] - x))[1]
    if role in {'row_number', 'project_code', 'row_code'} and re.search(r'[\u3400-\u9fff]{2,}', region.text):
        name_x = next(anchor for anchor, name in columns if name == 'raw_name')
        if x <= name_x <= right:
            # OCR can merge the printed serial/code and Chinese item label.
            # Keep the whole source region as a name candidate, with the usual
            # cross-column uncertainty, instead of dropping the item row.
            return 'raw_name'
    return role


def _headerless_groups(row, dictionary):
    # New item labels are anchors, not the count of values or a page midpoint.
    from .extraction import _result_and_tail, _is_unit, _FLAG
    from tools.sample_dictionary.normalize import normalize_candidate_name, is_rejected_candidate_name

    starts = []
    result_seen = False
    for index, region in enumerate(row):
        text = region.text.strip()
        name = normalize_candidate_name(text)
        known = bool(dictionary and dictionary.match(name))
        looks_name = bool(name and re.search(r'[A-Za-z\u4e00-\u9fff]', name) and not is_rejected_candidate_name(name))
        if _result_and_tail(text):
            result_seen = True
        elif (known or (result_seen and looks_name and not _is_unit(text, None)
                        and not _FLAG.fullmatch(text)
                        and index + 1 < len(row) and _result_and_tail(row[index + 1].text))) and not _header(region):
            previous_name = ' '.join(item.text for item in row[starts[-1] if starts else 0:index])
            combined_name = previous_name + ' ' + text
            same_label = bool(dictionary and dictionary.match(combined_name))
            if index == 0 or (not same_label and (result_seen or known)):
                starts.append(index)
                result_seen = False
    starts = sorted(set([0, *starts]))
    return [tuple(row[start:end]) for start, end in zip(starts, [*starts[1:], len(row)])]


def _table_context(contexts, left, right, fallback, *, multiple_tables=False):
    if not contexts or (len(contexts) == 1 and not multiple_tables):
        return fallback
    local = [entry for x, entry in contexts if left <= x < right]
    if len(local) == 1:
        return local[0]
    specimens, panels = ({entry[index] for entry in local if entry[index]} for index in (0, 1))
    if local and len(specimens) <= 1 and len(panels) <= 1:
        source = next((entry[2] for entry in local if entry[2]), ())
        return (next(iter(specimens), ''), next(iter(panels), ''), source,
                [reason for entry in local for reason in entry[3]])
    return ('', '', (), [quality_issue('association_conflict', ['specimen', 'raw_name'],
                                       '多表格上下文无法唯一归属，未借用另一栏的标本或面板。')])


def _item_row_anchors(page, dictionary, *, left=0, right=1, columns=()):
    from .extraction import _result_and_tail

    anchors = []
    for candidate in page.regions:
        x, start, end_x, end = _bounds(candidate)
        if not left <= x < right:
            continue
        if _header(candidate) or _result_and_tail(candidate.text):
            continue
        is_name = (_column_role(candidate, columns) == 'raw_name') if columns else bool(dictionary and dictionary.match(candidate.text))
        if is_name:
            anchors.append((start, end))
    return anchors


def _spans_item_rows(region, anchors):
    _x1, top, _x2, bottom = _bounds(region)
    bands = []
    for start, end in anchors:
        overlap = max(0, min(bottom, end) - max(top, start))
        if overlap / max(1e-9, min(bottom - top, end - start)) < .4:
            continue
        center = (start + end) / 2
        if all(abs(center - other) > max(end - start, height) * .6 for other, height in bands):
            bands.append((center, end - start))
    return len(bands) > 1


def associated_rows(pages, dictionary=None):
    from apps.processing.value_objects import OcrPage
    from .extraction import _is_unit, _FLAG

    scope_cache = {}
    templates = ()
    last_page = None
    specimen = panel = ''
    specimen_source = ()
    context_issues = []
    header_issues = {}
    contexts = []
    outside_lab = False
    ended_tables = set()
    for page in pages:
        if not isinstance(page, OcrPage):
            raise ValueError('Observation extraction requires OCR pages')
        continuation = bool(re.search(r'续表|接上页|continued', page.full_text, re.I))
        if last_page is None or page.page_number != last_page + 1 or not continuation:
            templates = ()
            specimen = panel = ''
            specimen_source = ()
            context_issues = []
            header_issues = {}
            contexts = []
            outside_lab = False
            ended_tables = set()
        last_page = page.page_number
        anchor_cache = {}
        for row in _rows(page):
            context_only = False
            for region in row:
                compact = re.sub(r'\s+', '', region.text)
                match = re.search(r'(?:标本(?:类型|种类)?|样本)[:：](全血|血液|血清|血浆|尿液|尿|粪便|大便)', compact)
                if match:
                    specimen = SPECIMENS[match.group(1)] if region.confidence >= .95 else ''
                    specimen_source = (page, region)
                    context_issues = [] if specimen else [quality_issue('recognition_uncertain', ['specimen'], '标本上下文置信度不足，未用于字典消歧。')]
                    panel = ''
                    context_only = True
                    x = _bounds(region)[0]
                    contexts = [(old_x, entry) for old_x, entry in contexts if abs(old_x - x) > .003]
                    contexts.append((x, (specimen, panel, specimen_source, list(context_issues))))
                for label, value in PANELS.items():
                    if compact in {label, label + '报告', label + '检验报告'}:
                        panel = value if region.confidence >= .95 else ''
                        if not panel:
                            context_issues.append(quality_issue('recognition_uncertain', ['raw_name'], '面板上下文置信度不足，未用于字典消歧。'))
                        context_only = True
                        x = _bounds(region)[0]
                        contexts = [(old_x, entry) for old_x, entry in contexts if abs(old_x - x) > .003]
                        contexts.append((x, (specimen, panel, specimen_source, list(context_issues))))
            new_templates = _templates(row)
            if new_templates:
                # Generic "item/result" columns also occur in specialist reports.
                # Reopening an ended scope needs laboratory-specific columns.
                templates, ended_tables, outside_lab, updated = _update_templates(templates, new_templates, ended_tables, outside_lab)
                header_issues = {key: reasons for key, reasons in header_issues.items()
                                 if key in {(left, right) for left, right, _ in templates}}
                for left, right in updated:
                    low_confidence = any(region.confidence < .95 for region in row
                                         if _header(region) and left <= _bounds(region)[0] < right)
                    header_issues[(left, right)] = [quality_issue('association_conflict', ['raw_name', 'raw_value', 'raw_unit', 'reference_range_raw'], '表头识别置信度不足，列关联需要核对。')] if low_confidence else []
                continue
            if context_only or all(re.fullmatch(r'(?:续表|接上页|continued)', x.text.strip(), re.I) for x in row):
                continue
            if not templates:
                if _non_lab_section(row):
                    outside_lab = True
                    specimen = panel = ''
                    specimen_source = ()
                    contexts = []
                    context_issues = []
                if outside_lab or _fragmented_without_cells(row, dictionary, scope_cache):
                    continue
                for group in _headerless_groups(row, dictionary):
                    local = _table_context(contexts, _bounds(group[0])[0] - .01, _bounds(group[-1])[2],
                                           (specimen, panel, specimen_source, context_issues))
                    group_issues = list(local[3])
                    if () not in anchor_cache:
                        anchor_cache[()] = _item_row_anchors(page, dictionary)
                    if any(_spans_item_rows(region, anchor_cache[()]) for region in group):
                        group_issues.append(quality_issue('association_conflict', ['raw_value'], '区域跨越多个项目行，无法唯一配对。'))
                    yield AssociatedRow(page, group, issues=group_issues, specimen=local[0], panel=local[1], specimen_source=local[2])
                continue
            for left, right, columns in templates:
                cells = [region for region in row if left <= _bounds(region)[0] < right]
                if not cells:
                    continue
                if _non_lab_section(cells):
                    ended_tables.add((left, right))
                    # Discard only this table's context. A placeholder prevents
                    # fallback to the specimen/panel of the other column.
                    contexts = [(x, entry) for x, entry in contexts if not left <= x < right]
                    contexts.append((left, ('', '', (), [])))
                    specimen = panel = ''
                    specimen_source = ()
                    context_issues = []
                if (left, right) in ended_tables:
                    continue
                anchor_key = (left, right, columns)
                if anchor_key not in anchor_cache:
                    anchor_cache[anchor_key] = _item_row_anchors(page, dictionary, left=left, right=right, columns=columns)
                local = _table_context(contexts, left, right, (specimen, panel, specimen_source, context_issues),
                                       multiple_tables=len(templates) > 1)
                fields, issues = {}, [*local[3], *header_issues.get((left, right), ())]
                for region in cells:
                    x = _bounds(region)[0]
                    if _FLAG.fullmatch(region.text.strip()) and 'report_flag_raw' not in {role for _, role in columns}:
                        fields.setdefault('report_flag_raw', []).append(region)
                        continue
                    if 'raw_unit' not in {role for _, role in columns} and _is_unit(region.text, None):
                        fields.setdefault('raw_unit', []).append(region)
                        continue
                    distances = sorted((abs(x - anchor), role) for anchor, role in columns)
                    distance = distances[0][0]
                    role = _column_role(region, columns)
                    if role != distances[0][1]:
                        issues.append(quality_issue('association_conflict', ['raw_name'], 'OCR 合并了序号或代号与项目名称，保留整个区域待核对。'))
                    if _bounds(region)[2] > right + .008 or any(
                        anchor > x + .008 and _bounds(region)[2] > anchor + .008
                        for anchor, other_role in columns if other_role != role
                    ):
                        issues.append(quality_issue('association_conflict', [role], '单元格区域横跨不同字段列，保留原文待核对。'))
                    if role != 'raw_name' and _spans_item_rows(region, anchor_cache[anchor_key]):
                        issues.append(quality_issue('association_conflict', [role], '单元格区域跨越多个项目行，无法唯一配对。'))
                    if len(distances) > 1 and abs(distance - distances[1][0]) < .012:
                        issues.append(quality_issue('association_conflict', [role, distances[1][1]], '单元格位于两个列锚点之间，字段归属待核对。'))
                    fields.setdefault(role, []).append(region)
                # Footer text or an orphan result has no report item to associate.
                if 'raw_name' not in fields:
                    continue
                if not fields.get('raw_value'):
                    issues.append(quality_issue('association_conflict', ['raw_value'], '项目所在行缺失结果单元格；未借用邻列或邻行结果。'))
                if len(fields.get('raw_value', ())) > 1:
                    joined = ' '.join(x.text for x in fields['raw_value'])
                    if not re.fullmatch(r'[<>≤≥]=?\s+[+-]?[\d.]+', joined):
                        issues.append(quality_issue('association_conflict', ['raw_value'], '结果列含多个候选单元格，保留原文待核对。'))
                yield AssociatedRow(page, tuple(cells), fields, issues, local[0], local[1], local[2])
