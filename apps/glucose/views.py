from django.core.paginator import Paginator
from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.documents.models import DocumentPage
from apps.labs.models import LabObservation
from apps.patients.access import Capability

from .forms import GlucoseRecordForm, HistoryFilterForm, LabImportForm, NursingImportForm, PRECISIONS, RevisionForm
from .history import display_row, history_charts
from .models import GlucoseRecord
from .payloads import GlucoseInputError, TIME_SLOTS
from .services import GlucoseConflict, create_record, import_lab_record, import_nursing_record, revise_record
from .sources import GlucoseSourceUnavailable, lab_candidate, nursing_page_candidate, preview_lab, preview_nursing_page, source_current


def _source_matches(patient, kind, identity, expected):
    if kind == 'LAB_REPORT':
        source = LabObservation.objects.select_related('parsing_version__document', 'document_page', 'evidence').filter(
            pk=identity, parsing_version__document__patient=patient).first()
        candidate = lab_candidate
    else:
        source = DocumentPage.objects.select_related('document').filter(pk=identity, document__patient=patient).first()
        candidate = nursing_page_candidate
    if source is None:
        return False
    try:
        return candidate(source)['source_fingerprint'] == expected
    except (GlucoseSourceUnavailable, GlucoseInputError):
        return False


def _render(request, template, context, *, status=200, watched=(), source_checks=()):
    response = render(request, template, {'current_section': 'glucose',
        'can_write': request.patient_access.permits(Capability.WRITE), **context}, status=status)
    for row in watched:
        record = row['record']
        current = GlucoseRecord.objects.filter(pk=record.pk, patient=request.patient).first()
        if (current is None or current.revision_number != record.revision_number or current.current_data != record.current_data
                or current.deleted_at != record.deleted_at or source_current(current) != row['source_available']):
            response.close()
            return protect_sensitive_html(HttpResponse('记录或来源已变化，请刷新后查看当前内容。', status=409))
    if any(not _source_matches(request.patient, *check) for check in source_checks):
        response.close()
        return protect_sensitive_html(HttpResponse('原件来源已变化，请刷新后重新核对。', status=409))
    return protect_sensitive_html(response)


def _record(request, identity):
    return get_object_or_404(GlucoseRecord.objects.select_related('created_by', 'updated_by', 'source_document')
                            .filter(patient=request.patient), pk=identity)


def _row(record):
    return display_row(record, source_available=source_current(record))


def _redirect(request, record):
    return redirect(reverse('glucose:detail', args=[record.pk]) + '?patient=' + str(request.patient.pk))


def _fields(data):
    zone = data['timezone'] or '时区未确认'
    if data['utc_offset']:
        zone += ' (UTC' + data['utc_offset'] + ')'
    if data['timezone_origin'] != 'UNCONFIRMED':
        zone += ' · ' + ('原件明确' if data['timezone_origin'] == 'SOURCE_EXPLICIT' else '用户确认')
    return [('原始结果', f"{data['raw_value']} {data['raw_unit']}".strip()),
            ('测量时间', data['local_time'].replace('T', ' ') if data['local_time'] else '时间不详'), ('时间精度', dict(PRECISIONS)[data['time_precision']]),
            ('所在时区', zone), ('时段', TIME_SLOTS[data['time_slot']]),
            ('设备或测量方式', data['source_label'] or '未填写'), ('备注', data['notes'] or '未填写')]


def _error(request, error, record=None):
    return _render(request, 'glucose/error.html', {'error': str(error), 'record': record}, status=409)


@patient_required
@require_GET
def index(request):
    form = HistoryFilterForm(request.GET)
    valid = form.is_valid()
    query = GlucoseRecord.objects.filter(patient=request.patient, deleted_at__isnull=True)
    if valid:
        selected = form.cleaned_data
        if selected.get('source_kind'):
            query = query.filter(source_kind=selected['source_kind'])
        if selected.get('source_label'):
            query = query.filter(current_data__source_label__icontains=selected['source_label'])
        if selected.get('time_slot'):
            query = query.filter(current_data__time_slot=selected['time_slot'])
        if selected.get('date_scope') == 'UNKNOWN':
            query = query.filter(measured_date__isnull=True)
        elif selected.get('date_scope') == 'DATED':
            query = query.filter(measured_date__isnull=False)
        if selected.get('start'):
            query = query.filter(measured_date__gte=selected['start'])
        if selected.get('end'):
            query = query.filter(measured_date__lte=selected['end'])
    else:
        query = query.none()
    query = query.order_by(F('measured_date').desc(nulls_last=True), F('measured_at').desc(nulls_last=True), 'pk')
    page = Paginator(query, 50).get_page(request.GET.get('page'))
    records = list(page.object_list)
    rows = [_row(record) for record in records]
    parameters = request.GET.copy()
    parameters.pop('page', None)
    parameters['patient'] = str(request.patient.pk)
    return _render(request, 'glucose/index.html', {'form': form, 'records': records, 'rows': rows,
        **history_charts(rows), 'page': page, 'filter_query': parameters.urlencode(),
        'filters_active': any(request.GET.get(key) for key in form.fields)}, status=200 if valid else 400, watched=rows)


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def create(request):
    form = GlucoseRecordForm(request.POST if request.method == 'POST' else None, source_kind='METER')
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                result = create_record(request.patient, request.user, form.payload(),
                    creation_key=form.cleaned_data['creation_key'], source_kind=form.cleaned_data['source_kind'])
            except (GlucoseInputError, GlucoseConflict) as error:
                form.add_error(None, str(error))
                status = 409 if isinstance(error, GlucoseConflict) else 400
            else:
                return _redirect(request, result.record)
    return _render(request, 'glucose/form.html', {'form': form, 'new_record': True, 'title': '记录血糖'}, status=status)


@patient_required
@require_GET
def detail(request, glucose_record_id):
    record = _record(request, glucose_record_id)
    row = _row(record)
    history = [{'revision': revision, 'before': _fields(revision.before['data']), 'after': _fields(revision.after['data'])}
               for revision in record.revisions.select_related('author').order_by('-sequence')]
    return _render(request, 'glucose/detail.html', {'record': record, 'row': row,
        'fields': _fields(record.current_data), 'original_fields': _fields(record.original_data),
        'history': history, 'revision_form': RevisionForm(initial={'expected_revision': record.revision_number})}, watched=[row])


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def edit(request, glucose_record_id):
    record = _record(request, glucose_record_id)
    row = _row(record)
    form = GlucoseRecordForm(request.POST if request.method == 'POST' else None, record=record)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                record = revise_record(request.patient, request.user, record.pk, action='CORRECT',
                    expected_revision=form.cleaned_data['expected_revision'], changes=form.payload())
            except (GlucoseInputError, GlucoseConflict) as error:
                form.add_error(None, str(error))
                status = 409 if isinstance(error, GlucoseConflict) else 400
            else:
                return _redirect(request, record)
    return _render(request, 'glucose/form.html', {'record': record, 'row': row, 'form': form,
        'new_record': False, 'title': '更正血糖记录'}, status=status, watched=[row] if request.method == 'GET' else [])


def _mutate(request, identity, action):
    record = _record(request, identity)
    form = RevisionForm(request.POST)
    status = 400
    if form.is_valid():
        try:
            record = revise_record(request.patient, request.user, record.pk, action=action,
                                    expected_revision=form.cleaned_data['expected_revision'])
        except (GlucoseInputError, GlucoseConflict) as error:
            form.add_error(None, str(error))
            status = 409 if isinstance(error, GlucoseConflict) else 400
        else:
            return _redirect(request, record)
    return _render(request, 'glucose/error.html', {'record': record, 'form': form}, status=status)


@patient_required(capability=Capability.WRITE)
@require_POST
def delete(request, glucose_record_id):
    return _mutate(request, glucose_record_id, 'DELETE')


@patient_required(capability=Capability.WRITE)
@require_POST
def undo(request, glucose_record_id):
    return _mutate(request, glucose_record_id, 'UNDO')


@patient_required
@require_GET
def sources(request):
    observations = LabObservation.objects.select_related('parsing_version__document', 'document_page', 'evidence').filter(
        parsing_version__document__patient=request.patient, parsing_version__document__deleted_at__isnull=True,
        parsing_version__active=True).order_by('-parsing_version__created_at', 'reading_order', 'pk')
    page = Paginator(observations, 100).get_page(request.GET.get('lab_page'))
    candidates = []
    source_checks = []
    for observation in page.object_list:
        try:
            candidate = lab_candidate(observation)
        except (GlucoseSourceUnavailable, GlucoseInputError):
            continue
        candidates.append({'observation': observation, 'data': candidate['data']})
        source_checks.append(('LAB_REPORT', observation.pk, candidate['source_fingerprint']))
    pages = Paginator(DocumentPage.objects.select_related('document').filter(document__patient=request.patient,
        document__deleted_at__isnull=True).order_by('-document__created_at', 'document_id', 'page_number'), 30).get_page(request.GET.get('original_page'))
    available_pages = []
    for original in pages.object_list:
        try:
            candidate = nursing_page_candidate(original)
        except GlucoseSourceUnavailable:
            continue
        available_pages.append(original)
        source_checks.append(('NURSING', original.pk, candidate['source_fingerprint']))
    pages.object_list = available_pages
    return _render(request, 'glucose/sources.html', {'candidates': candidates, 'lab_page': page, 'original_pages': pages},
                   source_checks=source_checks)


def _lab_import(request, observation_id, record=None):
    get_object_or_404(LabObservation.objects.filter(parsing_version__document__patient=request.patient), pk=observation_id)
    try:
        candidate = preview_lab(request.patient, request.user, observation_id)
    except GlucoseSourceUnavailable as error:
        return _error(request, error, record)
    form = LabImportForm(request.POST if request.method == 'POST' else None, candidate=candidate, record=record)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            data = form.cleaned_data
            try:
                result = import_lab_record(request.patient, request.user, observation_id,
                    expected_source=data['expected_source'], checked_original=data['checked_original'], creation_key=data['creation_key'],
                    confirm_timezone=data['confirm_timezone'], timezone_name=data['timezone'], utc_offset=data['utc_offset'],
                    recheck=record is not None, expected_revision=data.get('expected_revision'))
            except (GlucoseInputError, GlucoseConflict, GlucoseSourceUnavailable) as error:
                form.add_error(None, str(error))
                status = 400 if isinstance(error, GlucoseInputError) else 409
            else:
                return _redirect(request, result.record)
    source_url = reverse('labs:observation_source', args=[observation_id, 'raw_value']) + '?patient=' + str(request.patient.pk)
    return _render(request, 'glucose/import.html', {'form': form, 'record': record, 'candidate': candidate,
        'data': candidate['data'], 'fields': _fields(candidate['data']), 'source_url': source_url,
        'source_embed_url': source_url + '&embed=1',
        'title': '重新核对检验来源' if record else '核对并导入检验血糖', 'nursing': False}, status=status,
        watched=[_row(record)] if record else [],
        source_checks=[('LAB_REPORT', observation_id, candidate['source_fingerprint'])])


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def import_lab(request, observation_id):
    return _lab_import(request, observation_id)


def _nursing_import(request, document_id, page_number, record=None):
    page = get_object_or_404(DocumentPage.objects.filter(document__patient=request.patient),
                             document_id=document_id, page_number=page_number)
    try:
        candidate = preview_nursing_page(request.patient, request.user, page.pk)
    except GlucoseSourceUnavailable as error:
        return _error(request, error, record)
    form = NursingImportForm(request.POST if request.method == 'POST' else None, candidate=candidate, record=record)
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            data = form.cleaned_data
            try:
                result = import_nursing_record(request.patient, request.user, page.pk, form.payload(),
                    expected_source=data['expected_source'], checked_original=data['checked_original'], creation_key=data['creation_key'],
                    original_excerpt=data['original_excerpt'], measurement_scope=data['measurement_scope'],
                    confirm_timezone=data['confirm_timezone'], recheck_record_id=record.pk if record else None,
                    expected_revision=data.get('expected_revision'))
            except (GlucoseInputError, GlucoseConflict, GlucoseSourceUnavailable) as error:
                form.add_error(None, str(error))
                status = 400 if isinstance(error, GlucoseInputError) else 409
            else:
                return _redirect(request, result.record)
    source_url = reverse('documents:document_viewer', args=[page.document_id]) + f'?patient={request.patient.pk}&page={page.page_number}'
    image_url = reverse('documents:document_page_image', args=[page.document_id, page.page_number]) + f'?patient={request.patient.pk}'
    return _render(request, 'glucose/import.html', {'form': form, 'record': record, 'candidate': candidate,
        'source_url': source_url, 'source_image_url': image_url, 'source_page': page,
        'title': '重新核对护理原件' if record else '对照护理原件录入血糖', 'nursing': True}, status=status,
        watched=[_row(record)] if record else [],
        source_checks=[('NURSING', page.pk, candidate['source_fingerprint'])])


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def import_nursing(request, document_id, page_number):
    return _nursing_import(request, document_id, page_number)


@patient_required(capability=Capability.WRITE)
@require_http_methods(['GET', 'POST'])
def recheck(request, glucose_record_id):
    record = _record(request, glucose_record_id)
    if record.source_kind == 'LAB_REPORT' and record.source_observation_id:
        return _lab_import(request, record.source_observation_id, record)
    if record.source_kind == 'NURSING' and record.source_page_id:
        return _nursing_import(request, record.source_document_id, record.source_page.page_number, record)
    raise Http404('当前记录没有可重新核对的原件来源。')
