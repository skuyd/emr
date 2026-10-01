"""Presentation of the current user-entered record value."""


def display_row(record):
    data = record.current_data
    if record.kind == 'SYMPTOM':
        label = ' · '.join(value for value in (data['symptom_name'], data['severity']) if value)
    elif record.kind == 'ECOG':
        label = f"{data['score']} 分"
    else:
        label = f"{data['raw_value']} {data['raw_unit']}"
    return {'record': record, 'id': str(record.pk), 'label': label, 'data': data,
            'display_time': data['local_time'], 'kind_label': record.get_kind_display(),
            'time': record.record_time.strftime('%H:%M') if record.record_time else ''}
