from zoneinfo import ZoneInfo

from django.core.paginator import Paginator
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import Capability

from .forms import HistoryFilterForm, RecordForm, RevisionForm
from .history import display_row, history_series
from .models import DailyRecord
from .payloads import InvalidRecord, KINDS
from .services import RecordConflict, create_record, revise_record


def _render(request, template, context, *, status=200):
    return protect_sensitive_html(render(request, template, {
        'current_section': 'self_records', 'can_write': request.patient_access.permits(Capability.WRITE), **context,
    }, status=status))


def _record(request, record_id):
    return get_object_or_404(DailyRecord.objects.filter(patient=request.patient), pk=record_id)


def _detail_redirect(request, record):
    return redirect(reverse('self_records:detail', args=[record.pk]) + '?patient=' + str(request.patient.pk))


@patient_required
@require_GET
def index(request):
    data = request.GET.copy()
    data.setdefault('timezone', 'Asia/Shanghai')
    form = HistoryFilterForm(data)
    query = DailyRecord.objects.filter(patient=request.patient, deleted_at__isnull=True)
    valid = form.is_valid()
    zone_name = form.cleaned_data.get('timezone', 'Asia/Shanghai')
    if valid:
        if form.cleaned_data.get('kind'):
            query = query.filter(kind=form.cleaned_data['kind'])
        if form.cleaned_data.get('start') or form.cleaned_data.get('end'):
            query = query.annotate(display_day=TruncDate('measured_at', tzinfo=ZoneInfo(zone_name)))
        if form.cleaned_data.get('start'):
            query = query.filter(display_day__gte=form.cleaned_data['start'])
        if form.cleaned_data.get('end'):
            query = query.filter(display_day__lte=form.cleaned_data['end'])
    else:
        query = query.none()
    page = Paginator(query, 50).get_page(request.GET.get('page'))
    records = list(page.object_list)
    parameters = request.GET.copy()
    parameters.pop('page', None)
    parameters['patient'] = str(request.patient.pk)
    return _render(request, 'self_records/index.html', {
        'form': form, 'records': records, 'rows': [display_row(record, zone_name) for record in records],
        'charts': history_series(records, zone_name), 'page': page, 'filter_query': parameters.urlencode(), 'zone_name': zone_name,
    }, status=200 if valid else 400)


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def create(request):
    kind = request.GET.get('kind', 'WEIGHT')
    if kind not in KINDS:
        kind = 'WEIGHT'
    form = RecordForm(request.POST if request.method == 'POST' else None, kind=kind)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                result = create_record(request.patient, request.user, form.payload(), creation_key=form.cleaned_data['creation_key'])
            except (InvalidRecord, RecordConflict) as error:
                form.add_error(None, str(error))
                status = 409 if isinstance(error, RecordConflict) else 400
            else:
                return _detail_redirect(request, result.record)
    return _render(request, 'self_records/form.html', {'form': form, 'new_record': True}, status=status)


@patient_required
@require_GET
def detail(request, record_id):
    record = _record(request, record_id)
    return _render(request, 'self_records/detail.html', {
        'record': record, 'row': display_row(record, record.current_data['timezone']),
        'revision_form': RevisionForm(initial={'expected_revision': record.revision_number}),
        'history': record.revisions.select_related('author').order_by('-sequence'),
    })


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def edit(request, record_id):
    record = _record(request, record_id)
    form = RecordForm(request.POST if request.method == 'POST' else None, record=record)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                revised = revise_record(request.patient, request.user, record.pk, action='CORRECT',
                                        expected_revision=form.cleaned_data['expected_revision'], changes=form.payload())
            except (InvalidRecord, RecordConflict) as error:
                form.add_error(None, str(error))
                status = 409 if isinstance(error, RecordConflict) else 400
            else:
                return _detail_redirect(request, revised)
    return _render(request, 'self_records/form.html', {'record': record, 'form': form, 'new_record': False}, status=status)


def _mutate(request, record_id, action):
    record = _record(request, record_id)
    form = RevisionForm(request.POST)
    status = 400
    if form.is_valid():
        try:
            revised = revise_record(request.patient, request.user, record.pk, action=action,
                                    expected_revision=form.cleaned_data['expected_revision'])
        except (InvalidRecord, RecordConflict) as error:
            form.add_error(None, str(error))
            status = 409 if isinstance(error, RecordConflict) else 400
        else:
            return _detail_redirect(request, revised)
    return _render(request, 'self_records/error.html', {'record': record, 'form': form}, status=status)


@patient_required(capability=Capability.WRITE)
@require_POST
def delete(request, record_id):
    return _mutate(request, record_id, 'DELETE')


@patient_required(capability=Capability.WRITE)
@require_POST
def undo(request, record_id):
    return _mutate(request, record_id, 'UNDO')
