"""Associate OCR cells using observed headers; never compact away empty columns."""

from dataclasses import dataclass, field
import re
import unicodedata

from .candidates import _bounds, _rows


LAYOUT_RULE_VERSION = 'lab-layout-v2'
HEADERS = {
    'raw_name': {'项目', '项目名称', '检验项目', '检测项目', '名称', 'item', 'test'},
    'raw_value': {'结果', '检验结果', '检测结果', '测定值', 'result'},
    'raw_unit': {'单位', 'unit', 'units'},
    'reference_range_raw': {'参考值', '参考范围', '正常范围', '参考区间', 'reference', 'range'},
    'report_flag_raw': {'提示', '标志', '标记', '异常提示', 'flag'},
    'method_raw': {'方法', '检测方法', '测试方法', '测定方法', 'method'},
    'row_number': {'序号', '编号'},
    'project_code': {'项目代号', '项目代码', '英文名称', '缩写'},
    'row_code': {'序号代号'},
    'recognition_mark': {'互认标识'},
}
SPECIMENS = {'全血': 'BLOOD', '血液': 'BLOOD', '血清': 'BLOOD', '血浆': 'BLOOD', '尿液': 'URINE', '尿': 'URINE', '粪便': 'STOOL', '大便': 'STOOL'}
PANELS = {'血常规': 'CBC', '尿常规': 'URINALYSIS', '凝血功能': 'COAGULATION', '血凝': 'COAGULATION', '炎症指标': 'INFLAMMATION'}


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
    for region, role in recognized:
        if role in {item[1] for item in current}:
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
        is_name = (min(columns, key=lambda item: abs(item[0] - x))[1] == 'raw_name') if columns else bool(dictionary and dictionary.match(candidate.text))
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

    templates = ()
    last_page = None
    specimen = panel = ''
    specimen_source = ()
    context_issues = []
    header_issues = []
    contexts = []
    for page in pages:
        if not isinstance(page, OcrPage):
            raise ValueError('Observation extraction requires OCR pages')
        continuation = bool(re.search(r'续表|接上页|continued', page.full_text, re.I))
        if last_page is None or page.page_number != last_page + 1 or not continuation:
            templates = ()
            specimen = panel = ''
            specimen_source = ()
            context_issues = []
            header_issues = []
            contexts = []
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
                templates = new_templates
                header_issues = [] if all(region.confidence >= .95 for region in row if _header(region)) else [quality_issue('association_conflict', ['raw_name', 'raw_value', 'raw_unit', 'reference_range_raw'], '表头识别置信度不足，列关联需要核对。')]
                continue
            if context_only or all(re.fullmatch(r'(?:续表|接上页|continued)', x.text.strip(), re.I) for x in row):
                continue
            if not templates:
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
                anchor_key = (left, right, columns)
                if anchor_key not in anchor_cache:
                    anchor_cache[anchor_key] = _item_row_anchors(page, dictionary, left=left, right=right, columns=columns)
                cells = [region for region in row if left <= _bounds(region)[0] < right]
                if not cells:
                    continue
                local = _table_context(contexts, left, right, (specimen, panel, specimen_source, context_issues),
                                       multiple_tables=len(templates) > 1)
                fields, issues = {}, [*local[3], *header_issues]
                for region in cells:
                    x = _bounds(region)[0]
                    if _FLAG.fullmatch(region.text.strip()) and 'report_flag_raw' not in {role for _, role in columns}:
                        fields.setdefault('report_flag_raw', []).append(region)
                        continue
                    if 'raw_unit' not in {role for _, role in columns} and _is_unit(region.text, None):
                        fields.setdefault('raw_unit', []).append(region)
                        continue
                    distances = sorted((abs(x - anchor), role) for anchor, role in columns)
                    distance, role = distances[0]
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
