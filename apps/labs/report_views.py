"""Patient-scoped report identity review, separate from observation corrections."""

from functools import wraps
import uuid

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from .models import ReportAssociation
from .report_reads import read_report_identities
from .reports import (
    ReportDecisionConflict, correct_report, current_report_units, decide_relation,
    effective_report, ensure_historical_report_units, propose_relation, report_relations,
    relation_has_conflict, report_revision_history, report_revision_state, report_source_token, _from_snapshot,
)


def _render(request, template, context, *, status=200):
    return protect_sensitive_html(render(request, template, context, status=status))


def _errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ReportDecisionConflict as error:
            return _render(request, 'labs/error.html', {'error': str(error)}, status=409)
        except (ValueError, TypeError, ValidationError):
            return _render(request, 'labs/error.html', {'error': '输入无效，请选择原件位置、填写依据并检查内容。'}, status=400)
    return wrapped


def _report(unit, identity=None, *, relations=None):
    if identity is None:
        identity = read_report_identities(unit.parsing_version.document.patient, (unit,))[unit.pk]
    if relations is None:
        relations = report_relations(unit.parsing_version.document.patient)
    conflict = any(unit.source_key in (item.left_key, item.right_key) and relation_has_conflict(item) for item in relations)
    return {'unit': unit, 'identity': identity, 'report_conflict': conflict,
            'status': '待核对' if conflict and identity.status != 'REJECTED' else {'ACCEPTED': '已接纳', 'REVIEW': '待核对', 'REJECTED': '不满足接纳条件'}[identity.status]}


def _sources(unit):
    identity = effective_report(unit)
    choices, seen = [], set()
    labels = {'sampled_at': '采样时间', 'institution': '医院', 'report_number': '报告号', 'patient': '患者信息'}
    for name, label in labels.items():
        for source in identity.fields.get(name, ()):
            polygon = source.get('polygon')
            if not polygon or source.get('page_number') != unit.document_page.page_number:
                continue
            key = str(polygon)
            if key in seen:
                continue
            seen.add(key)
            choices.append({'label': f"{label}：{source.get('raw_text', '')}", 'page_number': unit.document_page.page_number,
                            'polygon': polygon, 'precision': 'region'})
    if identity.source_region:
        choices.append({'label': '本报告单元区域', 'page_number': unit.document_page.page_number,
                        'polygon': identity.source_region, 'precision': 'region'})
    elif not choices:
        choices.append({'label': '本报告原件整页（未识别到字段位置）', 'page_number': unit.document_page.page_number,
                        'polygon': [[0, 0], [1, 0], [1, 1], [0, 1]], 'precision': 'page'})
    return choices


@patient_required
@require_GET
@_errors
def report_list(request):
    ensure_historical_report_units(request.patient)
    relations = report_relations(request.patient)
    units = tuple(current_report_units(request.patient))
    identities = read_report_identities(request.patient, units)
    reports = {unit.source_key: _report(unit, identities[unit.pk], relations=relations) for unit in units}
    pairs = [{'association': item, 'left': reports[item.left_key], 'right': reports[item.right_key]}
             for item in relations if item.left_key in reports and item.right_key in reports]
    return _render(request, 'labs/reports.html', {'reports': tuple(reports.values()), 'relations': pairs,
                   'can_write': request.patient_access.permits('write'), 'current_section': 'comparison'})


@patient_required
@require_POST
@_errors
def relate_reports(request):
    left = get_object_or_404(current_report_units(request.patient), pk=request.POST.get('left'))
    right = get_object_or_404(current_report_units(request.patient), pk=request.POST.get('right'))
    association = propose_relation(request.patient, request.user, left.pk, right.pk)
    return redirect('labs:report_relation', association_id=association.pk)


@patient_required
@require_http_methods(['GET', 'POST'])
@_errors
def report_detail(request, unit_id):
    unit = get_object_or_404(current_report_units(request.patient), pk=unit_id)
    sources = _sources(unit)
    if request.method == 'POST':
        expected_source = request.POST.get('expected_source', '')
        if not expected_source:
            raise ValueError()
        index = int(request.POST.get('source_index', ''))
        if not 0 <= index < len(sources):
            raise ValueError()
        source = {key: value for key, value in sources[index].items() if key != 'label'}
        action = request.POST.get('decision', 'CORRECT')
        changes = {request.POST.get('field', ''): request.POST.get('value', '').strip()} if action == 'CORRECT' else {}
        correct_report(request.patient, request.user, unit.pk, changes,
            expected_revision=int(request.POST.get('expected_revision', '')), source_evidence=source,
            rationale=request.POST.get('rationale', '').strip(), operation_id=request.POST.get('operation_id', ''),
            expected_source=expected_source, action=action)
        return redirect('labs:report_detail', unit_id=unit.pk)
    from .readmodels import effective_rows

    rows = tuple(row for row in effective_rows(request.patient, version=unit.parsing_version,
                 include_uncertain=True, include_invalid=True) if row.report_unit_id == unit.pk)
    state = report_revision_state(unit)
    return _render(request, 'labs/report_detail.html', {**_report(unit), 'sources': sources, 'observations': rows,
        'source_token': report_source_token(unit, state), 'automatic': _from_snapshot(unit.automatic),
        'revision_conflict': state.identity.reason == 'report_revision_conflict', 'applied_revision': state.revision,
        'history': report_revision_history(unit), 'operation_id': uuid.uuid4(),
        'can_write': request.patient_access.permits('write'), 'current_section': 'comparison'})


@patient_required
@require_http_methods(['GET', 'POST'])
@_errors
def report_relation(request, association_id):
    association = get_object_or_404(ReportAssociation, pk=association_id, patient=request.patient)
    if request.method == 'POST':
        decide_relation(request.patient, request.user, association.pk, request.POST.get('action', ''),
            expected_revision=int(request.POST.get('expected_revision', '')), rationale=request.POST.get('rationale', '').strip(),
            operation_id=request.POST.get('operation_id', ''))
        return redirect('labs:report_relation', association_id=association.pk)
    report_relations(request.patient)
    association.refresh_from_db()
    units = {unit.source_key: unit for unit in current_report_units(request.patient).filter(
        source_key__in=(association.left_key, association.right_key))}
    if len(units) != 2:
        raise Http404()
    identities = read_report_identities(request.patient, units.values())
    return _render(request, 'labs/report_relation.html', {'association': association,
        'left': _report(units[association.left_key], identities[units[association.left_key].pk]),
        'right': _report(units[association.right_key], identities[units[association.right_key].pk]),
        'history': association.events.select_related('author').order_by('-sequence'), 'operation_id': uuid.uuid4(),
        'can_write': request.patient_access.permits('write'), 'current_section': 'comparison'})
