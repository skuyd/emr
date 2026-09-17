"""Pure display folding after patient, source permissions and selection filters.

No function deletes or edits an observation, or grants source access. Callers
must pass only their authorized scope; counts and latest times use those rows.
"""

from collections import defaultdict
from dataclasses import replace

from .comparison_policy import AbnormalResult
from .reports import result_identity


UNKNOWN_INSTITUTIONS = frozenset({'', '医院未识别', '多机构，待核对'})
_RESULT_FIELDS = frozenset({'raw_name', 'standard_code', 'raw_value', 'raw_unit', 'result_type', 'specimen'})
_UNCERTAIN = frozenset({'mapping_unknown', 'association_conflict', 'normalization_uncertain',
    'recognition_uncertain', 'type_conflict', 'unit_unknown', 'reported_error', 'revision_conflict',
    'specimen_conflict', 'internal_conflict', 'source_unavailable', 'source_policy_unknown'})


def institution_key(row):
    institution = getattr(row, 'comparison_institution', '')
    if institution in UNKNOWN_INSTITUTIONS:
        return ('unresolved', str(row.parsing_version.document_id), str(getattr(row, 'report_unit_id', '') or row.pk))
    return ('institution', institution)


def _fold_key(cell):
    row = cell.observation
    report = getattr(row, 'report_identity', None)
    specimen = row.specimen.strip().upper()
    if (report is None or report.status != 'ACCEPTED' or report.sampled_at is None
            or getattr(row, 'report_conflict', False)
            or institution_key(row)[0] != 'institution' or not cell.known_unit
            or not specimen or specimen in {'UNKNOWN', 'UNSPECIFIED'} or row.standard_code.startswith('CANDIDATE_')
            or any(item.get('code') in _UNCERTAIN and (not item.get('fields') or _RESULT_FIELDS.intersection(item['fields']))
                   for item in cell.quality_issues)):
        return ('source', str(row.pk))
    result = result_identity(row)
    if result is None:
        return ('source', str(row.pk))
    return (row.parsing_version.document.patient_id, report.sampled_at.date(), institution_key(row),
            row.standard_code, specimen, result)


def fold_cells(cells):
    groups = {}
    for cell in cells:
        groups.setdefault(_fold_key(cell), []).append(cell)
    output = []
    for group in groups.values():
        first = group[0]
        sources = tuple(source for cell in group for source in (cell.sources or (cell.observation,)))
        difference = len({(row.reference_range_raw.strip(), row.report_flag_raw.strip()) for row in sources}) > 1
        originals = tuple(original for cell in group for original in (cell.source_cells or (cell,)))
        comparability = first.comparability if all(cell.comparability == first.comparability for cell in group) else 'insufficient'
        output.append(replace(first, sources=sources, source_cells=originals, reference_difference=difference,
            trend_eligible=all(cell.trend_eligible for cell in group), plot_eligible=all(cell.plot_eligible for cell in group),
            comparability=comparability,
            comparability_label=first.comparability_label if comparability == first.comparability else '依据需逐来源核对',
            review_required=any(cell.review_required for cell in group),
            abnormal=AbnormalResult('review', '参考信息有差异', source='逐来源查看参考范围与标记') if difference else first.abnormal,
            reference_label='参考信息有差异' if difference else first.reference_label))
    return tuple(output)


def _daily_key(cell):
    row = cell.observation
    basis = cell.group_key[:4] + cell.group_key[5:]
    return (row.parsing_version.document.patient_id, row.observation_date, institution_key(row), basis)


def latest_daily_cells(cells):
    """Select within comparable hospital series, including uncertainty blockers.

    Return all disputed daily details. A late unreliable value cannot promote an
    earlier reliable one. Eligibility is checked by the plotter after selection.
    """
    groups = defaultdict(list)
    for displayed in cells:
        for cell in displayed.source_cells or (displayed,):
            cell = cell if cell.sources else replace(cell, sources=(cell.observation,))
            key = _daily_key(cell)
            report = getattr(cell.observation, 'report_identity', None)
            if key[1] is None and report is not None and report.sampling_dates:
                for day in report.sampling_dates:
                    groups[(key[0], day, *key[2:])].append(cell)
            else:
                groups[key].append(cell)
    selected, disputed = [], []
    for group in groups.values():
        if any(getattr(source, 'report_identity', None) is None
               or getattr(source, 'report_conflict', False)
               or source.report_identity.status != 'ACCEPTED' or source.report_identity.sampled_at is None
               for cell in group for source in cell.sources):
            disputed.extend(group)
            continue
        latest = max(cell.latest_sampled_at for cell in group)
        candidates = [cell for cell in group if cell.latest_sampled_at == latest]
        values = {result_identity(source) for cell in candidates for source in cell.sources
                  if source.report_identity.sampled_at == latest}
        if len(values) != 1 or None in values:
            disputed.extend(group)
            continue
        # Equal values at the latest time are one display point; all originals
        # remain available in daily detail, regardless of fold eligibility.
        equal_history = [cell for cell in group if result_identity(cell.observation) in values]
        # Keep the latest original's quality and method. Folding must not grant
        # an earlier record's trust to the latest measurement.
        representative = candidates[0]
        same_quality = [cell for cell in equal_history if cell.group_key == representative.group_key]
        ordered = [representative, *(cell for cell in same_quality if cell is not representative)]
        selected.append(fold_cells(ordered)[0])
    return tuple(selected), tuple(disputed)
