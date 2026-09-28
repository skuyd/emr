"""Patient-scoped report identity review, separate from observation corrections."""

from functools import wraps
import json
import re
from types import SimpleNamespace
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseGone, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.documents.models import Document, DocumentStatus
from .models import LabReportUnit, ObservationRevision, ReportAssociation
from .comparison import comparable_cell
from .catalog import PHASES
from .report_reads import read_report_identities, report_source_groups
from .report_workspace import report_workspace, submit_report_workspace
from .views import workflow_errors
from .revisions import RevisionConflict
from .reports import (
    ReportDecisionConflict, current_report_units, report_relations,
    relation_has_conflict, report_revision_history, report_revision_state,
    report_organization_token, organize_report_relation, _from_snapshot,
)


def _render(request, template, context, *, status=200):
    return protect_sensitive_html(render(request, template, context, status=status))


def _return_to(value, patient_id):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    if (parsed.scheme or parsed.netloc or not parsed.path.startswith('/') or '\\' in value
            or not url_has_allowed_host_and_scheme(value, allowed_hosts=set())
            or parse_qs(parsed.query).get('patient') != [str(patient_id)]):
        return ''
    return value if not parsed.fragment or re.fullmatch(r'result-[0-9a-f-]{36}', parsed.fragment) else value.split('#')[0]


def workspace_url(patient, *, unit_id=None, observation_id=None, return_to=''):
    workspace = report_workspace(patient)
    selected = next((report for report in workspace['reports']
                     if (unit_id and any(unit.pk == unit_id for unit in report['units']))
                     or (observation_id and any(row.pk == observation_id for row in report['rows']))), None)
    query = {'patient': str(patient.pk)}
    if selected:
        query['report'] = selected['key']
        if observation_id and any(row.pk == observation_id for row in selected['rows']):
            query['observation'] = str(observation_id)
    safe_return = _return_to(return_to, patient.pk)
    if safe_return:
        if observation_id and safe_return.startswith(reverse('labs:comparison') + '?'):
            safe_return = safe_return.split('#')[0] + f'#result-{observation_id}'
        query['return_to'] = safe_return
    return reverse('labs:report_workspace') + '?' + urlencode(query)


@patient_required
@require_http_methods(['GET', 'POST'])
def report_workspace_view(request):
    if request.method == 'POST':
        try:
            edits = json.loads(request.POST['edits'])
            intent = request.POST.get('intent')
            if intent not in {'save', 'confirm', 'confirm_next'}:
                raise ValidationError('请选择保存或确认操作。')
            result = submit_report_workspace(request.patient, request.user,
                request.POST.get('report_key', ''), request.POST.get('token', ''),
                request.POST.get('operation_id', ''), edits, confirm=intent != 'save')
            workspace = report_workspace(request.patient, selected_key=result['report_key'])
            next_key = workspace['next_key'] if intent == 'confirm_next' else result['report_key']
            query = {'patient': str(request.patient.pk)}
            if next_key:
                query['report'] = next_key
            safe_return = _return_to(request.GET.get('return_to', ''), request.patient.pk)
            if safe_return:
                query['return_to'] = safe_return
            next_url = reverse('labs:report_workspace') + '?' + urlencode(query)
            return JsonResponse({**result, 'next_url': next_url})
        except (KeyError, json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            message = '；'.join(error.messages) if isinstance(error, ValidationError) else '输入无效，请检查本报告内容。'
            return JsonResponse({'error': message}, status=400)
        except RevisionConflict as error:
            return JsonResponse({'error': str(error)}, status=409)
    selected_key = request.GET.get('report') or None
    workspace = report_workspace(request.patient, selected_key=selected_key)
    current = workspace['current']
    if selected_key and current is None:
        raise Http404()
    processing_count = (Document.objects.filter(patient=request.patient, deleted_at__isnull=True,
        status=DocumentStatus.PROCESSING).count() if current is None else 0)
    focus = request.GET.get('observation', '')
    if focus and current and focus not in {str(row.pk) for row in current['rows']}:
        raise Http404()
    row_items, conflicts, source_items = [], {}, []
    if current:
        source_items = [{**source, 'region_json': json.dumps(source['region']) if source['region'] else '',
                         'history': tuple(report_revision_history(source['unit']))}
                        for source in current['sources']]
        for row in current['rows']:
            source = next(source for source in current['sources'] if source['unit'].pk == row.report_unit_id)
            field_source = row.field_evidence.get('raw_value', {})
            polygon = field_source.get('polygon') if field_source.get('precision') == 'region' else None
            if (polygon is None and row.evidence.origin == 'AUTOMATIC'
                    and row.evidence.confidence is not None and row.evidence.confidence >= .9):
                polygon = row.evidence.polygon
            lineage = {row.pk, row.original_observation_id} | {
                item.get('observation_id') for item in row.value_sources.values() if isinstance(item, dict)}
            history = tuple(ObservationRevision.objects.filter(observation_id__in=lineage,
                observation__parsing_version__document_id=row.parsing_version.document_id)
                .select_related('author').order_by('-created_at', '-sequence'))
            row_items.append({'row': row, 'source': source, 'cell': comparable_cell(row, previous=current['rows']),
                              'history': history,
                              'polygon': polygon,
                              'polygon_json': json.dumps(polygon) if polygon else ''})
            if row.manual_conflict:
                identity = row.manual_identity or row.manual_counterpart
                conflicts.setdefault(str(identity), []).append(row)
    return _render(request, 'labs/report_workspace.html', {
        **workspace, 'focus_observation': focus, 'operation_id': uuid.uuid4(),
        'row_items': row_items, 'conflicts': conflicts, 'source_items': source_items,
        'phase_choices': PHASES,
        'retained_count': sum(not row.excluded and not row.manual_conflict for row in current['rows']) if current else 0,
        'excluded_count': sum(row.excluded for row in current['rows']) if current else 0,
        'can_write': request.patient_access.permits('write'),
        'processing_count': processing_count,
        'return_to': _return_to(request.GET.get('return_to', ''), request.patient.pk),
        'current_section': 'comparison',
    })


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
    pending = any(unit.source_key in (item.left_key, item.right_key) and item.state == 'REVIEW' for item in relations)
    return {'unit': unit, 'identity': identity, 'report_conflict': conflict,
            'source_pending': conflict or pending,
            'status': '待核对' if conflict and identity.status != 'REJECTED' else {'ACCEPTED': '已接纳', 'REVIEW': '待核对', 'REJECTED': '不满足接纳条件'}[identity.status]}


@patient_required
@require_GET
@_errors
def report_list(request):
    if request.GET.get('unit'):
        unit = get_object_or_404(current_report_units(request.patient), pk=request.GET['unit'])
        path = reverse('labs:report_organization', args=(unit.pk,))
    else:
        path = reverse('documents:records')
    return redirect(f'{path}?{urlencode({"patient": str(request.patient.pk)})}')


@patient_required
@require_http_methods(['GET', 'POST'])
@workflow_errors
def batch_confirmation(request):
    if request.method == 'POST':
        return _render(request, 'labs/legacy_retired.html',
            {'workspace_url': workspace_url(request.patient)}, status=410)
    return redirect(workspace_url(request.patient, return_to=request.GET.get('return_to', '')))


@patient_required
@require_POST
@_errors
def relate_reports(request):
    return HttpResponseGone('请从当前报告详情进入整理报告。')


@patient_required
@require_http_methods(['GET', 'POST'])
@_errors
def report_detail(request, unit_id):
    unit = get_object_or_404(current_report_units(request.patient), pk=unit_id)
    if request.method == 'POST':
        return _render(request, 'labs/legacy_retired.html',
            {'workspace_url': workspace_url(request.patient, unit_id=unit.pk)}, status=410)
    from .readmodels import effective_rows

    units = tuple(current_report_units(request.patient))
    group_keys = report_source_groups(request.patient, units=units)[unit.source_key]
    identities = read_report_identities(request.patient, units)
    report_sources = tuple(_organization_card(item, identities[item.pk]) for item in units
                           if item.source_key in group_keys)
    rows = tuple(row for row in effective_rows(request.patient, include_uncertain=True,
                 include_invalid=True) if row.report_unit_id and row.report_unit.source_key in group_keys)
    state = report_revision_state(unit)
    return _render(request, 'labs/report_detail.html', {**_report(unit), 'observations': rows,
        'workspace_url': workspace_url(request.patient, unit_id=unit.pk), 'automatic': _from_snapshot(unit.automatic),
        'revision_conflict': state.identity.reason == 'report_revision_conflict', 'applied_revision': state.revision,
        'history': report_revision_history(unit), 'report_sources': report_sources,
        'current_section': 'comparison'})


def _organization_card(unit, identity, label=''):
    region = identity.source_region
    style = ''
    if region:
        xs, ys = [point[0] for point in region], [point[1] for point in region]
        style = (f'left:{min(xs) * 100}%;top:{min(ys) * 100}%;'
                 f'width:{(max(xs) - min(xs)) * 100}%;height:{(max(ys) - min(ys)) * 100}%')
    return {'unit': unit, 'identity': identity, 'label': label, 'region_style': style}


def _organization_context(request, unit, *, form=None, error=None):
    units = tuple(current_report_units(request.patient))
    by_id = {str(item.pk): item for item in units}
    by_key = {item.source_key: item for item in units}
    relations = report_relations(request.patient)
    groups = report_source_groups(request.patient, units=units, relations=relations)
    current_keys = groups[unit.source_key]
    identities = read_report_identities(request.patient, units)
    by_pair = {frozenset((item.left_key, item.right_key)): item for item in relations}
    cards = {key: _organization_card(item, identities[item.pk]) for key, item in by_key.items()}
    current_sources = []
    for item in units:
        if item.source_key not in current_keys:
            continue
        relation = by_pair.get(frozenset((unit.source_key, item.source_key)))
        label = '当前选定报告' if item.pk == unit.pk else relation.get_state_display() if relation else '已关联来源'
        if any(item.source_key in (link.left_key, link.right_key) and relation_has_conflict(link)
               for link in relations):
            label += ' · 报告归属存在冲突'
        current_sources.append(_organization_card(item, identities[item.pk], label))
    current_sources.sort(key=lambda card: (str(card['unit'].parsing_version.document_id),
                                           card['unit'].document_page.page_number, card['unit'].ordinal))
    focused = None
    if form is None and request.GET.get('focus'):
        focused = next((item for item in relations if str(item.pk) == request.GET['focus']), None)
        if focused is None or not {focused.left_key, focused.right_key}.intersection(current_keys):
            raise Http404()
    affected_keys = set(current_keys)
    if focused:
        affected_keys.update(groups[focused.left_key])
        affected_keys.update(groups[focused.right_key])
    relevant = [item for item in relations if item.left_key in affected_keys or item.right_key in affected_keys]
    related, others = [], []
    for item in units:
        if item.source_key in current_keys:
            continue
        matching = [relation for relation in relevant if item.source_key in (relation.left_key, relation.right_key)
                    and {relation.left_key, relation.right_key}.intersection(current_keys)]
        focus = next((relation for relation in matching if relation.state == 'REVIEW' or relation_has_conflict(relation)),
                     matching[0] if matching else None)
        candidate = _organization_card(item, identities[item.pk],
            '待判断' if any(relation.state == 'REVIEW' or relation_has_conflict(relation)
                         for relation in matching) else matching[0].get_state_display() if matching else '')
        candidate['focus_relation'] = focus
        (related if candidate['label'] == '待判断' else others).append(candidate)
    selected_left = selected_right = selected_relation = None
    if form is not None:
        selected_left = by_id.get(form.get('left_id', ''))
        selected_right = by_id.get(form.get('right_id', ''))
        if selected_left and selected_right:
            selected_relation = by_pair.get(frozenset((selected_left.source_key, selected_right.source_key)))
    elif focused:
        selected_relation = focused
        selected_left = by_key[selected_relation.left_key]
        selected_right = by_key[selected_relation.right_key]
        if selected_left.source_key not in current_keys:
            selected_left, selected_right = selected_right, selected_left
    elif request.GET.get('target'):
        selected_left = unit
        selected_right = by_id.get(request.GET['target'])
        if selected_right is None or selected_right.source_key in current_keys:
            raise Http404()
        selected_relation = by_pair.get(frozenset((unit.source_key, selected_right.source_key)))
    selection_outside = bool(selected_left and selected_left.source_key not in current_keys)
    if selection_outside and form is None:
        selected_left = selected_right = None
    actions = ()
    if selected_left and selected_right and not selection_outside:
        if selected_relation and selected_relation.state in {'AUTO', 'SAME'}:
            choices = (('UNDO', '取消关联'),)
        else:
            choices = (('SAME', '属于同一报告'), ('DIFFERENT', '不是同一报告'))
        effects = []
        pair = frozenset((selected_left.source_key, selected_right.source_key))
        for action, label in choices:
            trial = [SimpleNamespace(left_key=item.left_key, right_key=item.right_key,
                state=('UNDONE' if action == 'UNDO' else action) if frozenset((item.left_key, item.right_key)) == pair
                      else item.state) for item in relations]
            if selected_relation is None:
                trial.append(SimpleNamespace(left_key=min(pair), right_key=max(pair), state=action))
            projected = report_source_groups(request.patient, units=units, relations=trial)[unit.source_key]
            effects.append({'action': action, 'label': label,
                'sources': tuple(cards[key] for key in sorted(projected)),
                'remaining_relations': sum(item.state in {'AUTO', 'SAME'} for item in trial
                    if item.left_key in current_keys or item.right_key in current_keys)})
        actions = tuple(effects)
    history = [event for item in relevant for event in item.events.select_related('author').order_by('-sequence')]
    history.sort(key=lambda event: (event.created_at, event.pk), reverse=True)
    return {'unit': unit, 'current_sources': current_sources, 'related_candidates': related,
        'other_candidates': others, 'relation_rows': tuple({'association': item,
            'left': cards[item.left_key], 'right': cards[item.right_key],
            'has_conflict': relation_has_conflict(item)} for item in relevant),
        'selected_left': cards[selected_left.source_key] if selected_left else None,
        'selected_right': cards[selected_right.source_key] if selected_right else None,
        'selected_relation': selected_relation, 'actions': actions, 'history': history,
        'selection_outside': selection_outside,
        'organization_token': report_organization_token(request.patient, units=units, relations=relations),
        'operation_id': uuid.uuid4(), 'rationale': form.get('rationale', '') if form is not None else '',
        'error': error, 'can_write': request.patient_access.permits('write'), 'current_section': 'records'}


@patient_required
@require_http_methods(['GET', 'POST'])
def report_organization(request, unit_id):
    unit = current_report_units(request.patient).filter(pk=unit_id).first()
    if unit is None:
        if request.method == 'POST' and LabReportUnit.objects.filter(
                pk=unit_id, parsing_version__document__patient=request.patient).exists():
            return _render(request, 'labs/report_organization_stale.html', {
                'error': '原件已删除或解析版本已变化，请从当前资料重新选择报告来源。',
                'rationale': request.POST.get('rationale', ''), 'current_section': 'records'}, status=409)
        raise Http404()
    if request.method == 'POST':
        try:
            association = organize_report_relation(request.patient, request.user, unit.pk,
                request.POST.get('left_id', ''), request.POST.get('right_id', ''), request.POST.get('action', ''),
                expected_context=request.POST.get('expected_context', ''),
                rationale=request.POST.get('rationale', '').strip(), operation_id=request.POST.get('operation_id', ''))
        except ReportDecisionConflict as exc:
            context = _organization_context(request, unit, form=request.POST, error=str(exc))
            return _render(request, 'labs/report_organization.html', context, status=409)
        except (ValueError, TypeError, ValidationError) as exc:
            context = _organization_context(request, unit, form=request.POST,
                error=str(exc) or '请选择来源并填写判断依据。')
            return _render(request, 'labs/report_organization.html', context, status=400)
        location = reverse('labs:report_organization', args=(unit.pk,))
        return redirect(f'{location}?{urlencode({"patient": str(request.patient.pk), "focus": str(association.pk)})}')
    return _render(request, 'labs/report_organization.html', _organization_context(request, unit))


@patient_required
@require_http_methods(['GET', 'POST'])
def report_relation(request, association_id):
    association = get_object_or_404(ReportAssociation, pk=association_id, patient=request.patient)
    if request.method == 'POST':
        return HttpResponseGone('旧关联表单已停用，请从当前报告详情进入整理报告。')
    units = {item.source_key: item for item in current_report_units(request.patient).filter(
        source_key__in=(association.left_key, association.right_key))}
    if len(units) != 2:
        raise Http404()
    document = request.GET.get('document', '')
    unit = next((item for item in units.values() if str(item.parsing_version.document_id) == document),
                units[association.left_key])
    query = {'patient': str(request.patient.pk), 'focus': str(association.pk),
             'document': str(unit.parsing_version.document_id)}
    return redirect(f'{reverse("labs:report_organization", args=(unit.pk,))}?{urlencode(query)}')
