from calendar import Calendar
from datetime import date, time
import re
from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import Capability

from .forms import RecordForm, RevisionForm
from .history import display_row
from .models import DailyRecord
from .payloads import InvalidRecord, KINDS
from .services import RecordConflict, create_record, revise_record


ECOG_GRADES = [
    ('0', '活动正常，不受限制。'),
    ('1', '剧烈活动受限；可行走，能做轻体力或坐位工作。'),
    ('2', '能完全自理，但不能工作；清醒时间中超过一半可离床活动。'),
    ('3', '仅能部分自理；清醒时间中超过一半卧床或坐椅。'),
    ('4', '完全不能自理，整日卧床或坐椅。'),
    ('5', '死亡。'),
]


def _render(request, template, context, *, status=200):
    return protect_sensitive_html(render(request, template, {
        'current_section': 'self_records', 'can_write': request.patient_access.permits(Capability.WRITE), **context,
    }, status=status))


def _record(request, record_id):
    return get_object_or_404(DailyRecord.objects.filter(patient=request.patient, deleted_at__isnull=True), pk=record_id)


def _entry_redirect(request, *, kind='WEIGHT', saved=None, corrected=False, deleted=False):
    view = request.POST.get('view', request.GET.get('view', 'calendar'))
    if view not in {'calendar', 'list'}:
        view = 'calendar'
    query = {'patient': str(request.patient.pk), 'kind': kind, 'view': view}
    if saved is not None:
        query['saved'] = str(saved)
    if corrected:
        query['corrected'] = '1'
    if deleted:
        query['deleted'] = '1'
    return redirect(reverse('self_records:create') + '?' + urlencode(query))


def _record_json(record):
    data = record.current_data
    row = display_row(record)
    return {'id': str(record.pk), 'kind': record.kind, 'kind_label': row['kind_label'],
            'date': record.record_date.isoformat(), 'time': row['time'], 'label': row['label'],
            'edit_url': reverse('self_records:edit', args=[record.pk]),
            'delete_url': reverse('self_records:delete', args=[record.pk]),
            'revision': record.revision_number, 'value': data.get('raw_value', ''),
            'unit': data.get('raw_unit', ''), 'symptom_name': data.get('symptom_name', ''),
            'severity': data.get('severity', ''), 'score': data.get('score')}


def _json_requested(request):
    return 'application/json' in request.headers.get('Accept', '')


def _entry_context(request, form, *, record=None):
    initial = form.initial
    data = request.POST if request.method == 'POST' else {}
    values = {key: data.get(key, initial.get(key, '')) for key in (
        'measured_local', 'record_date', 'value', 'unit', 'symptom_name', 'severity', 'score',
    )}
    values['record_date'] = str(values['record_date'] or '')
    values['score'] = str(values['score']) if values['score'] is not None else ''
    return {'values': values, 'unit_choices': form.fields['unit'].choices if 'unit' in form.fields else [],
            'ecog_grades': ECOG_GRADES, 'selected_kind': form.kind,
            'record_id': str(record.pk) if record else '',
            'return_view': request.GET.get('view', 'calendar') if request.method == 'GET'
            else request.POST.get('view', 'calendar')}


@patient_required
@require_GET
def index(request):
    try:
        today = date.fromisoformat(request.GET['today'])
        if not 2 <= today.year <= 9998:
            raise ValueError
    except (KeyError, ValueError):
        today = timezone.localdate()
    month_raw = request.GET.get('month', '')
    if re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', month_raw) and 2 <= int(month_raw[:4]) <= 9998:
        year, month = map(int, month_raw.split('-'))
    else:
        year, month = today.year, today.month
    month_start = date(year, month, 1)
    next_start = date(year + (month == 12), month % 12 + 1, 1)
    previous_start = date(year - (month == 1), 12 if month == 1 else month - 1, 1)
    month_key = month_start.strftime('%Y-%m')
    try:
        selected = date.fromisoformat(request.GET['date'])
    except (KeyError, ValueError):
        selected = today if today.year == year and today.month == month else month_start
    kind = request.GET.get('kind', '')
    if kind not in KINDS:
        kind = ''
    view_mode = request.GET.get('view', 'calendar')
    if view_mode not in {'calendar', 'list'}:
        view_mode = 'calendar'
    weeks = Calendar(firstweekday=0).monthdatescalendar(year, month)
    query = DailyRecord.objects.filter(patient=request.patient, deleted_at__isnull=True,
                                       record_date__range=(weeks[0][0], weeks[-1][-1]))
    if kind:
        query = query.filter(kind=kind)
    records = sorted(query, key=lambda record: (record.record_date, record.record_time is not None,
                                                record.record_time or time.min, str(record.pk)), reverse=True)
    rows_by_day = {}
    for record in records:
        rows_by_day.setdefault(record.record_date, []).append(display_row(record))
    month_count = sum(len(rows) for day, rows in rows_by_day.items() if day.year == year and day.month == month)
    calendar_weeks = []
    for week in weeks:
        cells = []
        for day in week:
            rows = rows_by_day.get(day, [])
            counts = [(label, sum(row['record'].kind == code for row in rows))
                      for code, label in DailyRecord.Kind.choices]
            cells.append({'date': day, 'in_month': day.month == month, 'today': day == today,
                          'selected': day == selected, 'count': len(rows),
                          'summaries': [(label, count) for label, count in counts if count]})
        calendar_weeks.append(cells)
    list_groups = [{'date': day, 'rows': rows_by_day[day]} for day in sorted(rows_by_day, reverse=True)
                   if day.year == year and day.month == month]

    def browse_url(*, month_value=month_key, day_value=selected.isoformat(), mode=view_mode):
        return reverse('self_records:index') + '?' + urlencode({
            'patient': str(request.patient.pk), 'month': month_value, 'date': day_value,
            'kind': kind, 'view': mode, 'today': today.isoformat(),
        })

    return _render(request, 'self_records/index.html', {
        'records': records, 'calendar_weeks': calendar_weeks, 'day_rows': rows_by_day.get(selected, []),
        'list_groups': list_groups, 'month_count': month_count, 'month_key': month_key,
        'selected_day': selected, 'today': today, 'kind': kind, 'view_mode': view_mode,
        'kind_choices': [('', '全部类型'), *DailyRecord.Kind.choices],
        'previous_month': previous_start.strftime('%Y-%m'), 'next_month': next_start.strftime('%Y-%m'),
        'previous_url': browse_url(month_value=previous_start.strftime('%Y-%m'), day_value=previous_start.isoformat()),
        'next_url': browse_url(month_value=next_start.strftime('%Y-%m'), day_value=next_start.isoformat()),
        'calendar_url': browse_url(mode='calendar'), 'list_url': browse_url(mode='list'),
        'today_url': browse_url(month_value=today.strftime('%Y-%m'), day_value=today.isoformat()),
    })


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
                if _json_requested(request):
                    return protect_sensitive_html(JsonResponse({'record': _record_json(result.record),
                                                                'created': result.created,
                                                                'next_creation_key': str(uuid4())}))
                return _entry_redirect(request, kind=result.record.kind, saved=result.record.pk)
        if _json_requested(request):
            return protect_sensitive_html(JsonResponse({'errors': form.errors.get_json_data()}, status=status))
    saved = None
    if request.GET.get('saved'):
        try:
            saved_id = UUID(request.GET['saved'])
        except ValueError:
            pass
        else:
            saved = DailyRecord.objects.filter(pk=saved_id, patient=request.patient,
                                               deleted_at__isnull=True).first()
    return _render(request, 'self_records/form.html', {'form': form, 'new_record': True,
        'saved_row': display_row(saved) if saved else None,
        'corrected': request.GET.get('corrected') == '1', 'deleted': request.GET.get('deleted') == '1',
        **_entry_context(request, form)}, status=status)


@patient_required
@require_GET
def existing(request):
    kind = request.GET.get('kind', '')
    raw_date = request.GET.get('date', '')
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        day = None
    if kind not in KINDS or day is None or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', raw_date):
        return protect_sensitive_html(JsonResponse({'error': '请选择有效日期和记录类型。'}, status=400))
    records = DailyRecord.objects.filter(patient=request.patient, record_date=day, kind=kind,
                                         deleted_at__isnull=True)
    records = sorted(records, key=lambda record: (record.record_time is not None,
                                                   record.record_time or time.min, str(record.pk)), reverse=True)
    return protect_sensitive_html(JsonResponse({'records': [_record_json(record) for record in records]}))


@patient_required
@require_GET
def detail(request, record_id):
    record = _record(request, record_id)
    return _render(request, 'self_records/detail.html', {
        'record': record, 'row': display_row(record),
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
                if _json_requested(request):
                    return protect_sensitive_html(JsonResponse({'record': _record_json(revised)}))
                return _entry_redirect(request, kind=revised.kind, saved=revised.pk, corrected=True)
        if _json_requested(request):
            return protect_sensitive_html(JsonResponse({'errors': form.errors.get_json_data()}, status=status))
    return _render(request, 'self_records/form.html', {'record': record, 'form': form, 'new_record': False,
        **_entry_context(request, form, record=record)}, status=status)


@patient_required(capability=Capability.WRITE)
@require_POST
def delete(request, record_id):
    record = _record(request, record_id)
    if request.POST.get('confirm') != 'delete':
        return protect_sensitive_html(HttpResponseBadRequest('请先确认删除。'))
    form = RevisionForm(request.POST)
    status = 400
    if form.is_valid():
        try:
            revise_record(request.patient, request.user, record.pk, action='DELETE',
                                    expected_revision=form.cleaned_data['expected_revision'])
        except (InvalidRecord, RecordConflict) as error:
            form.add_error(None, str(error))
            status = 409 if isinstance(error, RecordConflict) else 400
        else:
            if _json_requested(request):
                return protect_sensitive_html(JsonResponse({'deleted': True, 'id': str(record.pk)}))
            return _entry_redirect(request, kind=record.kind, deleted=True)
    if _json_requested(request):
        return protect_sensitive_html(JsonResponse({'errors': form.errors.get_json_data()}, status=status))
    return _render(request, 'self_records/error.html', {'record': record, 'form': form}, status=status)
