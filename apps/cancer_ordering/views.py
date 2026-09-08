from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import authorize_patient, Capability

from .forms import ACTION_LABELS, ASSERTION_LABELS, MODE_LABELS, STATUS_LABELS, SUBJECT_LABELS, CandidateForm, SelectionForm, SelectionGuardForm
from .models import CancerCandidate, DisplaySelection
from .profiles import PROFILE_LABELS
from .readmodels import resolve_ordering
from .services import OrderingConflict, collect_current, revise_candidate, select_ordering, undo_selection


REASONS = {
    'collection_incomplete': '部分当前报告还未完成表述收集。请重新收集后查看；暂用通用顺序。',
    'no_reported_diagnosis': '当前没有可用于排列的明确报告表述。',
    'reported_diagnoses_differ': '报告表述存在不同癌种，请核对原件或明确选择显示顺序。',
    'unsupported_reported_diagnosis': '当前表述尚无对应的指标顺序。',
    'original_review_required': '部分表述或来源仍需对照原件核对，暂用通用顺序。',
    'reported_diagnosis': '根据当前报告中的明确表述自动排列。',
    'selected_reported_diagnosis': '使用你明确选择的报告表述。',
    'manual_display_preference': '使用你手动选择的显示顺序。',
    'explicit_general': '使用你选择的通用顺序。',
    'selection_source_changed': '原先选择的来源已经变化，暂用通用顺序。请核对后重新选择。',
    'selection_author_or_configuration_changed': '原先选择的记录已失效，暂用通用顺序。请重新选择。',
}


def _changed():
    return protect_sensitive_html(HttpResponse('报告来源、核对记录或显示选择已变化，请刷新后查看。', status=409))


def _redirect(request, candidate_id=None):
    path = reverse('cancer_ordering:detail', args=[candidate_id]) if candidate_id else reverse('cancer_ordering:index')
    response = HttpResponseRedirect(path + '?patient=' + str(request.patient.pk))
    response.status_code = 303
    return response


def _display(row):
    return {**row, 'status_label': STATUS_LABELS[row['status']],
            'assertion_label': ASSERTION_LABELS[row['content']['assertion']],
            'subject_label': SUBJECT_LABELS[row['content']['subject']],
            'profile_label': PROFILE_LABELS.get(row['content']['profile'], '尚无对应顺序')}


def _history(aggregate, request):
    if aggregate is None:
        return ()
    rows = []
    for event in aggregate.revisions.order_by('-sequence'):
        after = event.after
        if isinstance(aggregate, DisplaySelection):
            description = MODE_LABELS[after['mode']]
            if after['mode'] == 'MANUAL_PROFILE':
                description += ' · ' + PROFILE_LABELS[after['profile']]
            action = '撤销上次选择' if event.action == 'UNDO' else '选择显示顺序'
        else:
            content = after['content']
            description = ' · '.join((content['label'], ASSERTION_LABELS[content['assertion']],
                                       SUBJECT_LABELS[content['subject']], STATUS_LABELS[after['status']]))
            action = ACTION_LABELS[event.action]
        rows.append({'sequence': event.sequence, 'action': action, 'description': description,
            'author': '已注销成员' if event.author_id is None else '本人' if event.author_id == request.user.pk else '家庭成员',
            'created_at': event.created_at, 'reason': getattr(event, 'reason', '')})
    return tuple(rows)


def _render(request, template, state, context=None, *, status=200):
    response = render(request, template, {'state': state, 'mode_label': MODE_LABELS[state['mode']],
        'profile_label': PROFILE_LABELS[state['profile']], 'reason': REASONS[state['reason']],
        'can_write': request.patient_access.permits(Capability.WRITE), 'current_section': 'profile',
        **(context or {})}, status=status)
    try:
        # Invalid POST responses also contain patient data and bound form
        # choices. Authorize and compare the same initial material after render.
        authorize_patient(request.patient.pk, request.user, Capability.WRITE if request.method == 'POST' else Capability.READ)
        current = resolve_ordering(request.patient)
    except ObjectDoesNotExist:
        response.close()
        return _changed()
    except Exception:
        response.close()
        raise
    if current['fingerprint'] != state['fingerprint']:
        response.close()
        return _changed()
    return protect_sensitive_html(response)


def _index(request, *, undoing=False):
    try:
        state = resolve_ordering(request.patient)
    except ObjectDoesNotExist:
        return _changed()
    form = SelectionForm(request.POST if request.method == 'POST' and not undoing else None, state=state)
    undo_form = SelectionGuardForm(request.POST if undoing else None, state=state)
    status = 200
    if request.method == 'POST':
        active_form = undo_form if undoing else form
        status = 400
        if active_form.is_valid():
            data = active_form.cleaned_data
            try:
                guards = {'actor': request.user, 'expected_revision': data['expected_revision'],
                          'expected_fingerprint': data['expected_fingerprint']}
                if undoing:
                    undo_selection(request.patient, **guards)
                else:
                    select_ordering(request.patient, **guards, mode=data['mode'],
                        profile=data['profile'] if data['mode'] == 'MANUAL_PROFILE' else '',
                        candidate_id=data['candidate_id'] if data['mode'] == 'CANDIDATE' else None)
            except (ValidationError, OrderingConflict) as error:
                active_form.add_error(None, error)
                status = 409 if isinstance(error, OrderingConflict) else 400
            else:
                return _redirect(request)
    visible = {str(pk) for pk in CancerCandidate.objects.filter(patient=request.patient,
        document__deleted_at__isnull=True).values_list('pk', flat=True)}
    selection = DisplaySelection.objects.filter(patient=request.patient).first()
    return _render(request, 'cancer_ordering/index.html', state, {'form': form, 'undo_form': undo_form,
        'rows': [_display(row) for row in state['candidates'] if row['id'] in visible],
        'history': _history(selection, request)}, status=status)


@patient_required
@require_http_methods(['GET', 'POST'])
def index(request):
    return _index(request)


@patient_required
@require_http_methods(['GET', 'POST'])
def detail(request, cancer_candidate_id):
    candidate = get_object_or_404(CancerCandidate.objects.filter(patient=request.patient,
        document__deleted_at__isnull=True), pk=cancer_candidate_id)
    try:
        state = resolve_ordering(request.patient)
    except ObjectDoesNotExist:
        return _changed()
    row = next((row for row in state['candidates'] if row['id'] == str(candidate.pk)), None)
    if row is None:
        return _changed()
    form = CandidateForm(request.POST if request.method == 'POST' else None, row=row)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            data = form.cleaned_data
            try:
                revise_candidate(request.patient, candidate.pk, actor=request.user, action=data['action'],
                    expected_revision=data['expected_revision'], expected_source=data['expected_source'],
                    checked_original=data['checked_original'], reason=data['reason'], changes=form.changes())
            except (ValidationError, OrderingConflict) as error:
                form.add_error(None, error)
                status = 409 if isinstance(error, OrderingConflict) else 400
            else:
                return _redirect(request, candidate.pk)
    return _render(request, 'cancer_ordering/detail.html', state,
        {'row': _display(row), 'candidate': candidate, 'form': form, 'history': _history(candidate, request)}, status=status)


@patient_required
@require_POST
def collect(request):
    collect_current(request.patient, actor=request.user)
    return _redirect(request)


@patient_required
@require_POST
def undo(request):
    return _index(request, undoing=True)
