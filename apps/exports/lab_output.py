"""Frozen display groups with selection-local source counts and references."""

from copy import deepcopy
from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace

from apps.facts.readmodels import digest
from apps.labs.consolidation import UNKNOWN_INSTITUTIONS, fold_cells
from apps.labs.reports import result_identity


def _report_groups(keys, relations):
    groups = {key: {key} for key in keys}
    joined = {frozenset((item['left'], item['right'])) for item in relations
              if item['state'] in {'AUTO', 'SAME'} and item['left'] in keys and item['right'] in keys}
    for pair in sorted(joined, key=lambda pair: tuple(sorted(pair))):
        a, b = sorted(pair)
        left, right = groups[a], groups[b]
        if left is right or not all(frozenset((x, y)) in joined for x in left for y in right):
            continue
        combined = left | right
        for key in combined:
            groups[key] = combined
    return {key: min(group) for key, group in groups.items()}


def _disputed_sources(labs, conflicted_keys):
    daily = defaultdict(list)
    for row in labs:
        basis = row['comparison']['group_key']
        institution = row['institution'] if row['institution'] not in UNKNOWN_INSTITUTIONS else row['report']['source_key']
        dates = [row['date']['value']] if row['date']['value'] else row['report'].get('sampling_dates') or [None]
        for day in dates:
            daily[(day, institution, tuple(basis[:4] + basis[5:]))].append(row)
    disputed = set()
    for rows in daily.values():
        uncertain = any(row['report']['status'] != 'ACCEPTED' or not row['report']['sampling_time']
                        or row['report']['source_key'] in conflicted_keys for row in rows)
        if not uncertain:
            latest = max(datetime.fromisoformat(row['report']['sampling_time']) for row in rows)
            values = {result_identity(SimpleNamespace(result_type=row['result_type'], raw_value=row['value'], raw_unit=row['unit']))
                      for row in rows if datetime.fromisoformat(row['report']['sampling_time']) == latest}
            uncertain = len(values) != 1 or None in values
        if uncertain:
            disputed.update(row['id'] for row in rows)
    return disputed


def project_lab_output(labs, results, relations=()):
    """Rebuild every derived value after filtering; never copy a wider count/clock."""
    by_id = {row['id']: row for row in labs}
    keys = {row['report']['source_key'] for row in labs}
    relations = [deepcopy(item) for item in relations if item['left'] in keys and item['right'] in keys]
    conflicted_keys = {key for item in relations if item.get('conflict') for key in (item['left'], item['right'])}
    disputed = _disputed_sources(labs, conflicted_keys)
    groups = _report_groups(keys, relations)
    if all(row['comparison'].get('fold_key') for row in labs):
        partitions = {}
        for row in labs:
            key = row['id'] if row['report']['source_key'] in conflicted_keys else row['comparison']['fold_key']
            partitions.setdefault(key, []).append(row['id'])
        results = [{'source_ids': ids} for ids in partitions.values()]
    columns, output = {}, []
    for result in results:
        sources = [by_id[key] for key in result['source_ids'] if key in by_id]
        if not sources:
            continue
        first = sources[0]
        day, institution = first['date']['value'], first['institution']
        unresolved = first['report']['source_key'] if day is None or institution in UNKNOWN_INSTITUTIONS else ''
        column_id = digest([day, institution, unresolved])
        column = columns.setdefault(column_id, {'id': column_id, 'date': day, 'institution': institution,
                                                'result_ids': [], 'source_ids': []})
        times = [row['report']['sampling_time'] for row in sources if row['report']['sampling_time']]
        references = {(row['reference_range_raw'].strip(), row['raw_report_flag'].strip()) for row in sources}
        standard_basis = bool(first.get('catalog_code') and all(
            (row.get('catalog_code'), row.get('standard_reference'), row.get('physiological_phase')) ==
            (first['catalog_code'], first.get('standard_reference'), first.get('physiological_phase')) for row in sources))
        source_ids = [row['id'] for row in sources]
        item = {'id': min(source_ids), 'column_id': column_id, 'date': day, 'institution': institution,
                'standard_code': first['standard_code'], 'name': first['standard_name'] or first['name'],
                'value': first['value'], 'unit': first['unit'], 'result_type': first['result_type'],
                'standard_reference': first.get('standard_reference', ''),
                'standard_reference_unit': first.get('standard_reference_unit', ''),
                'physiological_phase': first.get('physiological_phase', ''),
                'source_ids': source_ids, 'source_count': len(sources),
                'disputed': bool(set(source_ids) & disputed),
                'report_count': len({groups[row['report']['source_key']] for row in sources}),
                'image_count': len({(row['document_id'], row['page']) for row in sources}),
                'latest_sampling_time': max(times, key=datetime.fromisoformat) if times else '',
                'reference_difference': len(references) > 1,
                'reference_label': ('参考信息有差异' if len(references) > 1 and not standard_basis else ('' if standard_basis else '无法对照')
                    if any(row['report']['source_key'] in conflicted_keys for row in sources) else first['reference_label'])}
        output.append(item)
        column['result_ids'].append(item['id'])
        column['source_ids'].extend(source_ids)
    for column in columns.values():
        sources = [by_id[key] for key in column.pop('source_ids')]
        column.update(report_count=len({groups[row['report']['source_key']] for row in sources}),
                      image_count=len({(row['document_id'], row['page']) for row in sources}),
                      result_count=len(column['result_ids']))
    return {'lab_columns': sorted(columns.values(), key=lambda item: (item['date'] is None, item['date'] or '', item['institution'], item['id'])),
            'lab_results': output, 'lab_report_relations': relations}


def build_lab_output(patient, observations, labs):
    from apps.labs.reports import relation_has_conflict, report_relations

    by_id = {str(row.pk): row.export_cell for row in observations}
    cells = [by_id[row['id']] for row in labs]
    results = [{'source_ids': [str(row.pk) for row in cell.sources]} for cell in fold_cells(cells)]
    relations = [{'left': item.left_key, 'right': item.right_key, 'state': item.state,
                  'conflict': relation_has_conflict(item),
                  'revision': item.revision_number} for item in report_relations(patient)]
    return project_lab_output(labs, results, relations)


def lab_sections(snapshot, *, selected_ids=None):
    labs = [row for row in snapshot['labs'] if selected_ids is None or row['id'] in selected_ids]
    projected = project_lab_output(labs, snapshot.get('lab_results', []), snapshot.get('lab_report_relations', []))
    filenames = {row['id']: row['filename'] for row in snapshot['documents']}
    by_id = {row['id']: {**row, 'filename': filenames[row['document_id']],
                        'other_field_pages': sorted({proof['page_number'] for proof in row['field_evidence'].values()
                                                     if proof.get('page_number') and proof['page_number'] != row['page']})}
             for row in labs}
    results = {item['id']: {**item, 'sources': [by_id[key] for key in item['source_ids']]}
               for item in projected['lab_results']}
    return [{**column, 'results': [results[key] for key in column['result_ids']]} for column in projected['lab_columns']]
