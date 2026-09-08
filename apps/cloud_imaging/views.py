from functools import wraps
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.encoding import iri_to_uri
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import authorize_patient

from .forms import DECISIONS, DecisionForm, ManualSourceForm, OpenForm, ScanForm
from .readmodels import document_snapshot, source_details
from .scan_services import request_scan
from .services import CloudConflict, add_manual_source, open_source, revise_source, visit_source


_render = render
STATUS_LABELS = {'PENDING': '待核对', 'CONFIRMED': '已核对', 'EXCLUDED': '已排除', 'STALE': '来源已变化',
                 'QUEUED': '等待扫描', 'RUNNING': '正在扫描', 'SUCCEEDED': '扫描完成', 'FAILED': '部分或全部扫描未完成', 'INVALIDATED': '扫描输入或访问已变化'}
KIND_LABELS = {'OCR': '原始文字', 'QR': '本地二维码', 'MANUAL': '人工原页转录'}


def _private_view(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except PermissionDenied:
            raise Http404('来源不可用。') from None
        except Http404:
            raise
        except Exception:
            # Keep a failing request and its stack visible, without copying an
            # arbitrary renderer/database exception containing a private URL.
            raise RuntimeError('cloud_source_request_failed') from None
    return wrapped


def _changed():
    return protect_sensitive_html(HttpResponse('原件、来源或核对选项已变化，请刷新后重新核对。', status=409))


def _redirect(name, identity, patient_id):
    response = HttpResponse(status=303)
    response['Location'] = reverse(name, args=[identity]) + '?' + urlencode({'patient': str(patient_id)})
    return protect_sensitive_html(response)


def _decorate(material):
    for source in material['sources']:
        source['status_label'] = STATUS_LABELS[source['status']]
        source['kind_label'] = KIND_LABELS[source['evidence']['kind']]
    for scan in material['scans']:
        scan['status_label'] = STATUS_LABELS[scan['status']]


@patient_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
@_private_view
@sensitive_variables()
def document(request, document_id):
    material = document_snapshot(request.patient, actor=request.user, document_id=document_id)
    read_token = material['read_token']
    action = request.POST.get('action') if request.method == 'POST' else None
    scan_form = ScanForm(request.POST if action == 'SCAN' else None, material=material, prefix='scan')
    manual_form = ManualSourceForm(request.POST if action == 'ADD' else None, material=material, prefix='manual')
    status, error = 200, ''
    if request.method == 'POST':
        form = scan_form if action == 'SCAN' else manual_form if action == 'ADD' else None
        status = 400
        if form is not None and form.is_valid():
            try:
                if action == 'SCAN':
                    request_scan(request.patient, actor=request.user, document_id=document_id, **form.cleaned_data)
                    return _redirect('cloud_imaging:document', document_id, request.patient.pk)
                source = add_manual_source(request.patient, actor=request.user, document_id=document_id, **form.cleaned_data)
                return _redirect('cloud_imaging:source', source.pk, request.patient.pk)
            except (ValidationError, CloudConflict) as failure:
                form.add_error(None, failure)
                status = 409 if isinstance(failure, CloudConflict) else 400
        elif form is None:
            error = '请选择有效的来源操作。'
    # The body and both forms were built from this same snapshot. Refreshing
    # only material here would give old form choices a new dependency token.
    _decorate(material)
    response = _render(request, 'cloud_imaging/document.html', {'material': material, 'scan_form': scan_form, 'manual_form': manual_form,
        'can_write': request.patient_access.permits('write'), 'error': error, 'current_section': 'records'}, status=status)
    if document_snapshot(request.patient, actor=request.user, document_id=document_id)['read_token'] != read_token:
        return _changed()
    return protect_sensitive_html(response)


@patient_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
@_private_view
@sensitive_variables()
def source(request, source_id):
    row = source_details(request.patient, actor=request.user, source_id=source_id)
    material = document_snapshot(request.patient, actor=request.user, document_id=row['document_id'])
    row_token, document_token = row['read_token'], material['read_token']
    form = DecisionForm(request.POST if request.method == 'POST' else None, material=material, row=row)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            data = form.cleaned_data
            changes = None
            if data['action'] in {'CORRECT', 'RECHECK'}:
                changes = {key: data[key] for key in ('url', 'title', 'report_id')}
                if data['page_id']:
                    changes['page_id'] = data['page_id']
            elif data['action'] == 'REASSIGN':
                changes = {'report_id': data['report_id']}
            try:
                revise_source(request.patient, actor=request.user, source_id=source_id, changes=changes,
                    **{key: data[key] for key in ('action', 'expected_revision', 'expected_source', 'operation_id', 'checked_original')})
                return _redirect('cloud_imaging:source', source_id, request.patient.pk)
            except (ValidationError, CloudConflict) as failure:
                form.add_error(None, failure)
                status = 409 if isinstance(failure, CloudConflict) else 400
    # An error form retains its original row and option snapshot. The final
    # checks discard the entire response if validation raced with a change.
    query = {'patient': str(request.patient.pk), 'page': row['evidence']['page']}
    if row['source_valid']:
        query.update(cloud_source=row['id'], cloud_evidence=row['evidence']['id'], cloud_token=row['source_token'])
    viewer_url = reverse('documents:document_viewer', args=[row['document_id']]) + '?' + urlencode(query)
    row['status_label'], row['kind_label'] = STATUS_LABELS[row['status']], KIND_LABELS[row['evidence']['kind']]
    for revision in row['history']:
        revision['action_label'] = dict(DECISIONS).get(revision['action'], '新增来源')
    response = _render(request, 'cloud_imaging/source.html', {'row': row, 'material': material, 'form': form, 'viewer_url': viewer_url,
        'can_write': request.patient_access.permits('write'), 'current_section': 'records'}, status=status)
    if (source_details(request.patient, actor=request.user, source_id=source_id)['read_token'] != row_token
            or document_snapshot(request.patient, actor=request.user, document_id=row['document_id'])['read_token'] != document_token):
        return _changed()
    return protect_sensitive_html(response)


def _visit_headers(response):
    protect_sensitive_html(response)
    response['Referrer-Policy'] = 'no-referrer'
    response['Cross-Origin-Opener-Policy'] = 'same-origin'
    return response


@sensitive_variables()
def _external_redirect(target, *, scripted=False):
    # Fetch must not follow a medical URL. A successful same-origin navigation
    # response exposes Location only after explicit POST; native clients retain
    # 303. Both forms share exactly the same final authorization/source check.
    response = HttpResponse(status=200 if scripted else 303)
    response['Location'] = iri_to_uri(target.value)
    return _visit_headers(response)


def _visit_changed(request, source_id):
    # This recovery page contains only internal identities and static text.
    response = _render(request, 'cloud_imaging/visit_changed.html',
                       {'source_id': source_id, 'current_section': 'records'}, status=409)
    try:
        authorize_patient(request.patient, request.user, 'read')
    except Exception:
        response.close()
        raise
    return _visit_headers(response)


@sensitive_variables()
def _notice_response(request, summary, *, status=200):
    # Never render a bound form: invalid hidden input can itself contain an
    # arbitrary URL. Both the body and hidden fields use this initial snapshot.
    response = _render(request, 'cloud_imaging/visit.html',
        {'summary': summary, 'invalid_submission': status == 400, 'current_section': 'records'}, status=status)
    try:
        current = visit_source(request.patient, actor=request.user, source_id=summary['id'])
        if current['read_token'] != summary['read_token']:
            raise CloudConflict('来源已变化。')
    except Exception:
        response.close()
        raise
    return _visit_headers(response)


@patient_required(capability='read')
@require_http_methods(['GET', 'HEAD'])
@_private_view
@sensitive_variables()
def visit(request, source_id):
    try:
        summary = visit_source(request.patient, actor=request.user, source_id=source_id)
        return _notice_response(request, summary, status=400 if set(request.GET) - {'patient'} else 200)
    except CloudConflict:
        return _visit_changed(request, source_id)


@patient_required(capability='read')
@require_http_methods(['POST'])
@sensitive_post_parameters()
@_private_view
@sensitive_variables()
def open(request, source_id):
    try:
        summary = visit_source(request.patient, actor=request.user, source_id=source_id)
        form = OpenForm(request.POST)
        if not form.is_valid() or set(request.GET) - {'patient'}:
            return _notice_response(request, summary, status=400)
        data = form.cleaned_data
        target = open_source(request.patient, actor=request.user, source_id=source_id, **data)
        response = _external_redirect(target, scripted=request.headers.get('X-Cloud-Open') == 'navigate')
        try:
            # Includes actual actor and every confirmation/source/author
            # dependency, after the response (including Location) is built.
            open_source(request.patient, actor=request.user, source_id=source_id, **data)
        except Exception:
            response.close()
            raise
        return response
    except CloudConflict:
        return _visit_changed(request, source_id)
