"""Portable glucose tables, scoped sharing and printable selected entries."""

from copy import deepcopy

from apps.exports.errors import ExportInputError
from apps.exports.selection import identifiers

from .exporting import DATA_KEYS
from .payloads import TIME_SLOTS


ARRAYS = ('glucose_records', 'glucose_record_sources')
RECORD_META = ('id', 'source_kind', 'source_kind_label', 'created_by', 'updated_by', 'created_at',
               'updated_at', 'revision_number', 'revision_id', 'revision_author', 'original_data')
VALUE_FIELDS = ('schema_version', 'raw_value', 'raw_unit', 'normalized_value', 'normalized_unit', 'conversion',
                'result_type', 'measured_at', 'measured_date', 'local_time', 'measured_local_raw',
                'time_precision', 'timezone', 'timezone_origin', 'utc_offset', 'time_slot',
                'plot_eligible', 'unplottable_reason', 'source_label', 'notes', 'field_origins')
CSV_FIELDS = {
    'glucose_records': (*RECORD_META, *VALUE_FIELDS),
    'glucose_record_sources': ('record_id', 'source_kind', 'document_id', 'page_id', 'page_number',
                             'parsing_version_id', 'observation_id', 'source_fingerprint',
                             'specimen_raw', 'sampling', 'reporting', 'field_sources'),
}


def csv_content(data):
    return {
        'glucose_records': [{**{key: row.get(key) for key in RECORD_META},
                             **{key: row['data'].get(key) for key in VALUE_FIELDS}}
                            for row in data.get('glucose_records', [])],
        'glucose_record_sources': deepcopy(data.get('glucose_record_sources', [])),
    }


def bind_output(output, snapshot, *, sharing=False):
    from .models import GlucoseExportSource, GlucoseShareSource

    model, target = (GlucoseShareSource, 'share') if sharing else (GlucoseExportSource, 'job')
    model.objects.bulk_create([
        model(**{target: output, 'record_id': row['id']}) for row in snapshot.get('glucose_records', [])
    ])


def bindings_current(output, snapshot):
    selected = identifiers(snapshot.get('selection', {}).get('glucose_record_ids', []))
    expected = [row['id'] for row in snapshot.get('glucose_records', [])]
    actual = [str(identity) for identity in output.glucose_sources.values_list('record_id', flat=True)]
    return len(expected) == len(selected) and set(expected) == set(selected) == set(actual)


def share_material(snapshot, scope):
    selected = set(scope.get('glucose_record_ids', [])) if 'glucose' in scope['sections'] else set()
    records = [row for row in snapshot.get('glucose_records', []) if row['id'] in selected]
    sources = [row for row in snapshot.get('glucose_record_sources', []) if row['record_id'] in selected]
    if selected != {row['id'] for row in records} or selected != {row['record_id'] for row in sources}:
        raise ExportInputError('部分选定血糖记录已变化。')
    # Source locations identify the selected measurement; they grant no access
    # to the ordinary record/history routes or the whole source document.
    source_keys = {'record_id', 'source_kind', 'document_id', 'page_id', 'page_number',
                   'specimen_raw', 'sampling', 'reporting'}
    return {
        'glucose_records': [{
            **{key: deepcopy(row[key]) for key in ('id', 'source_kind', 'source_kind_label', 'revision_number')},
            'data': {key: deepcopy(value) for key, value in row['data'].items() if key in DATA_KEYS},
        } for row in records],
        'glucose_record_sources': [{key: deepcopy(value) for key, value in row.items() if key in source_keys}
                                   for row in sources],
        'glucose_fingerprint': snapshot.get('glucose_fingerprint'),
        'glucose_document_ids': deepcopy(snapshot.get('glucose_document_ids', [])),
    }


def _time(data):
    precision = {'SECOND': '秒', 'MINUTE': '分钟', 'DAY': '日', 'MONTH': '月', 'YEAR': '年', 'UNKNOWN': '不详'}
    local = data.get('local_time') or data.get('measured_local_raw') or '时间不详'
    result = f"测量时间：{local}；时间精度：{precision.get(data.get('time_precision'), '不详')}"
    if data.get('timezone_origin') == 'UNCONFIRMED':
        result += '；时区未确认，未转换为 UTC'
    elif data.get('timezone'):
        result += '；' + data['timezone']
        if data.get('utc_offset'):
            result += '（UTC' + data['utc_offset'] + '）'
        result += '；原件明确时区' if data.get('timezone_origin') == 'SOURCE_EXPLICIT' else '；时区由用户确认'
    return result


def shared_rows(snapshot):
    """Display the selected current projection without author or history metadata."""
    sources = {row['record_id']: row for row in snapshot.get('glucose_record_sources', [])}
    source_keys = {'document_id', 'page_number', 'specimen_raw', 'sampling', 'reporting'}
    return [{
        'source_kind_label': row['source_kind_label'], 'revision_number': row['revision_number'],
        'data': {key: deepcopy(value) for key, value in row['data'].items() if key in DATA_KEYS},
        'time_description': _time(row['data']),
        'time_slot_label': TIME_SLOTS.get(row['data'].get('time_slot'), '未注明'),
        'source': {key: deepcopy(value) for key, value in sources.get(row['id'], {}).items() if key in source_keys},
    } for row in snapshot.get('glucose_records', [])]


def card_entries(snapshot):
    selected = set(snapshot['card'].get('glucose_record_ids', []))
    sources = {row['record_id']: row for row in snapshot.get('glucose_record_sources', [])}
    slots = dict(TIME_SLOTS)
    entries = []
    for row in snapshot.get('glucose_records', []):
        if row['id'] not in selected:
            continue
        data, source = row['data'], sources.get(row['id'], {})
        text = f"{row['source_kind_label']}：{data['raw_value']} {data['raw_unit'] or '单位未注明'}；" + _time(data)
        text += '；时段：' + slots.get(data.get('time_slot'), '未注明')
        if data.get('normalized_value') is not None:
            text += f"；换算值：{data['normalized_value']} {data['normalized_unit']}"
            if data.get('conversion', {}).get('formula'):
                text += '（' + data['conversion']['formula'] + '）'
        if not data.get('plot_eligible') and data.get('unplottable_reason'):
            text += '；不进入曲线：' + data['unplottable_reason']
        if source.get('document_id'):
            text += f"；来源原件 {source['document_id']} 第 {source.get('page_number') or '不详'} 页"
        if source.get('specimen_raw'):
            text += '；标本：' + source['specimen_raw']
        for key, label in (('sampling', '原件采样时间'), ('reporting', '原件报告时间')):
            stamp = source.get(key, {})
            if stamp.get('local') or stamp.get('raw'):
                text += f"；{label}：{stamp.get('local') or stamp['raw']}"
                if stamp.get('timezone_origin') == 'UNCONFIRMED':
                    text += '（时区未确认）'
        text += f"；测量方式：{data.get('source_label') or '未填写'}；备注：{data.get('notes') or '未填写'}"
        text += f"；记录人：{row.get('created_by') or '已注销账号'}；最近修改人：{row.get('updated_by') or '已注销账号'}"
        text += f"；记录编号 {row['id']}，修订 {row['revision_number']}。"
        entries.append({'text': text})
    return entries
