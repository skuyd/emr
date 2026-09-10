from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from apps.patients.share_views import _shared_view

from .forms import OpenForm
from .services import CloudConflict
from .sharing import open_shared_source, visit_shared_source
from .views import _external_redirect, _visit_headers


@sensitive_variables()
def _notice(request, share_id, summary, *, status=200):
    response = _visit_headers(render(request, 'cloud_imaging/shared_visit.html',
        {'share_id': share_id, 'summary': summary, 'invalid_submission': status == 400}, status=status))
    try:
        access, current = visit_shared_source(share_id, request.user, request.session.session_key, summary['id'])
        request.share_access = access
        if current['read_token'] != summary['read_token']:
            raise CloudConflict('分享来源已变化。')
    except Exception:
        response.close()
        raise
    return response


@_shared_view
@require_http_methods(['GET', 'HEAD'])
@sensitive_variables()
def visit(request, share_id, source_id):
    access, summary = visit_shared_source(share_id, request.user, request.session.session_key, source_id)
    request.share_access = access
    return _notice(request, share_id, summary, status=400 if request.GET else 200)


@_shared_view
@require_http_methods(['POST'])
@sensitive_post_parameters()
@sensitive_variables()
def open(request, share_id, source_id):
    access, summary = visit_shared_source(share_id, request.user, request.session.session_key, source_id)
    request.share_access = access
    form = OpenForm(request.POST)
    if not form.is_valid() or request.GET or 'patient_id' in request.POST:
        return _notice(request, share_id, summary, status=400)
    try:
        access, target = open_shared_source(share_id, request.user, request.session.session_key, source_id, **form.cleaned_data)
        request.share_access = access
        response = _external_redirect(target, scripted=request.headers.get('X-Cloud-Open') == 'navigate')
        try:
            open_shared_source(share_id, request.user, request.session.session_key, source_id, **form.cleaned_data)
        except Exception:
            response.close()
            raise
        return response
    except CloudConflict:
        return _visit_headers(HttpResponse('分享来源已变化，请返回分享后重新打开。', status=409))
