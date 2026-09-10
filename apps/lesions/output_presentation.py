"""Only frozen selected rows enter printable and shared lesion displays."""
from .comparison import _point_blockers
from .portable import point_value
from .presentation import LIMIT_LABELS
from .trends import AXIS_LABELS


def card_sections(snapshot):
    sections = snapshot.get('selection', {}).get('sections', ['imaging'])
    if 'imaging' not in sections or not snapshot.get('lesions'):
        return []
    fields = {row['id']: row for row in snapshot.get('clinical_fields', [])}
    observations = {row['id']: row for row in snapshot.get('lesion_observations', [])}
    entries = [{'text': '人工确认的观察分组；保留报告原文及算术对照，不作为医学结论。'}]
    for lesion in snapshot['lesions']:
        entries.append({'text': f"{lesion['name']}；标识 {lesion['id']}，名称修订 {lesion['revision_number']}。"})
        for observation in observations.values():
            if observation['lesion_id'] != lesion['id']:
                continue
            site = ' / '.join(fields[identity]['content']['text'] for identity in observation['site_field_ids'])
            entries.append({'text': f"原文部位：{site}；观察 {observation['id']}，关联修订 {observation['revision_number']}。"})
            for side_id in (*observation['laterality_scope']['whole_field_ids'], *observation['laterality_scope']['named_field_ids']):
                entries.append({'text': fields[side_id]['content']['text']})
            if observation['laterality_scope']['unknown_field_ids']:
                entries.append({'text': '原侧别作用范围未记录，未用作整体同侧或异侧依据。'})
        for row in snapshot.get('lesion_measurements', []):
            if row['lesion_id'] != lesion['id']:
                continue
            point = point_value(row)
            axis = 'SUVmax' if point.kind == 'SUV' else AXIS_LABELS.get(point.axis, f'第 {point.component_index + 1} 个尺寸（轴未明确）')
            role = {'CURRENT': '本次测量', 'HISTORICAL': '原文引用历史值', 'UNKNOWN': '时间角色不详'}.get(point.role, '时间角色不详')
            text = f'{point.date_value or "日期未选或不详"}；{point.method[1] or "方法未选或不详"}；{axis}；{role}；原文 {point.raw_expression}。'
            if point.kind == 'DIMENSION':
                text += f' 第 {point.component_index + 1} 个有序数值：{point.raw_value} {point.raw_unit or "单位未注明"}。'
            if point.conversion:
                text += f' {point.raw_value} {point.raw_unit} × {point.conversion["factor"]} = {point.value} {point.unit}（公制长度换算）。'
            if point.report_maximum:
                text += ' 所选原文明确为本报告最大。'
            for reason in _point_blockers(point):
                text += ' ' + LIMIT_LABELS.get(reason, '存在未核对限制。')
            text += f' 字段 {point.field_id}。'
            entries.append({'text': text})
            comparison = row['comparison']
            if comparison['comparable']:
                entries.append({'text': f'与选定相邻测量的算术差值（后次减前次）：{comparison["delta"]} {point.unit or ""}；不转换为疗效判断。'})
            elif comparison['previous_id']:
                entries.append({'text': '相邻测量未作差值：' + ' '.join(LIMIT_LABELS.get(reason, '所选来源范围不足。') for reason in comparison['reasons'])})
    return [{'key': 'lesions', 'title': '选定病灶观察与测量', 'included': True, 'entries': entries}]
