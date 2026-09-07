from collections import defaultdict
from copy import deepcopy
from functools import wraps
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms import HiddenInput, formset_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import authorize_patient

from . import cycles, regimens
from .derivations import decide_proposal, persist_proposals, proposal_preview
from .forms import AssignmentForm, CycleForm, DecisionForm, EventForm, MergeForm, ProposalForm, RegimenForm, SplitPartForm
from .models import TreatmentCycle, TreatmentEvent, TreatmentRegimen
from .overlays import METRICS, build_cycle_overlays, chart_groups
from .readmodels import event_token, treatment_material
from .records import assign_record, record_state
from .services import TreatmentConflict, create_manual_event, revise_event
from .timeline import build_cycle_timeline
from .workspace import workspace_material


STATUSES = {"PENDING": "尚待确认", "CONFIRMED": "已确认", "REJECTED": "已拒绝", "SUPERSEDED": "已被替代", "STALE": "来源或依据已变化"}
ACTIONS = {"CREATE": "本人补记", "CONFIRM": "确认", "CORRECT": "更正", "REJECT": "拒绝", "REVOKE": "撤销确认",
           "MERGE": "被合并", "MERGE_RESULT": "合并所得", "SPLIT": "被拆分", "SPLIT_RESULT": "拆分所得", "ASSIGN_RECORD": "检查归属更正"}


def _coherent_read(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method in {"GET", "HEAD"}:
            with transaction.atomic():
                authorize_patient(request.patient, request.user, "read", lock=True)
                return view(request, *args, **kwargs)
        return view(request, *args, **kwargs)
    return wrapped


def _render(request, template, context, status=200):
    return protect_sensitive_html(render(request, template, {"current_section": "records",
        "can_write": request.patient_access.permits("write"), **context}, status=status))


def _error(form, exc):
    form.add_error(None, exc if isinstance(exc, ValidationError) else str(exc))
    return 409 if isinstance(exc, TreatmentConflict) else 400


def _available(rows):
    return [row for row in rows if row["source_valid"] and row["status"] not in {"REJECTED", "SUPERSEDED"}]


def _decorate(row):
    row["status_label"] = STATUSES[row["status"]]
    row["origin_label"] = "原文自动提议" if row["origin"] == "AUTOMATIC" else "本人补记或明确组织"
    return row


def _material(request, *, full=False):
    data = workspace_material(request.patient, actor=request.user, include_history=True) if full else treatment_material(request.patient, actor=request.user, include_history=True)
    for rows in [data["events"], data["regimens"], data["cycles"]]:
        for row in rows:
            _decorate(row)
    return data


def _read(request, kind, identity, *, full=False):
    model = {"event": TreatmentEvent, "regimen": TreatmentRegimen, "cycle": TreatmentCycle}[kind]
    instance = get_object_or_404(model, patient=request.patient, pk=identity)
    material = _material(request, full=full)
    row = next(row for row in material[kind + "s"] if row["id"] == str(instance.pk))
    history = next(row for row in material["history"] if row["kind"] == kind and row["id"] == str(instance.pk))
    for revision in history["revisions"]:
        revision["action_label"] = ACTIONS.get(revision["action"], revision["action"])
    return instance, row, material, history


def _tokens(row, material, kind):
    if kind == "event":
        return {source["id"]: source["current_source_token"] for source in row["sources"]}
    identities = {link["event_id"] for link in row["event_links"]} if kind == "cycle" else set(row["content"].get("event_tokens", {}))
    return {event["id"]: event_token({key: value for key, value in event.items() if key not in {"origin_label", "status_label"}})
            for event in material["events"] if event["id"] in identities}


def _initial(row, material, kind):
    return {**deepcopy(row["content"]), "operation_id": uuid.uuid4(), "expected_revision": row["revision_number"],
            "expected_sources": _tokens(row, material, kind), "regimen_id": row.get("regimen_id") or "",
            "event_ids": [link["event_id"] for link in row.get("event_links", [])]}


def _confirm_unchanged(action, changes, original):
    if action == "CONFIRM" and any((value or None) != (original.get(key) or None) for key, value in changes.items()):
        raise ValidationError("表单内容已有修改，请使用更正并确认。")


@patient_required
@require_http_methods(["GET", "POST"])
@_coherent_read
def index(request):
    error_form, status = None, 200
    if request.method == "POST":
        error_form = ProposalForm(request.POST)
        if error_form.is_valid():
            data = error_form.cleaned_data
            try:
                if request.POST.get("action") == "PERSIST":
                    persist_proposals(request.patient, actor=request.user, expected_fingerprint=data["expected_fingerprint"], operation_id=data["operation_id"])
                    return redirect("treatments:index")
                if request.POST.get("action") not in {"CONFIRM_PROPOSAL", "REJECT_PROPOSAL"}:
                    raise ValidationError("请选择有效的提议操作。")
                revision = decide_proposal(request.patient, data["proposal_id"], actor=request.user,
                    expected_fingerprint=data["expected_fingerprint"], expected_revision=data["expected_revision"] or 0,
                    operation_id=data["operation_id"], checked_original=data["checked_original"],
                    action="CONFIRM" if request.POST["action"] == "CONFIRM_PROPOSAL" else "REJECT")
                return redirect("treatments:cycle", cycle_id=revision.cycle_id)
            except (TreatmentConflict, ValidationError) as exc:
                status = _error(error_form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    data = _material(request, full=True)
    preview = proposal_preview(request.patient, actor=request.user)
    for row in preview["proposals"]["cycles"]:
        row["form"] = ProposalForm(initial={"expected_fingerprint": preview["input_fingerprint"], "proposal_id": row["id"],
                                            "expected_revision": row["revision_number"]}, auto_id=f'proposal-{row["id"]}-%s')
    layout = request.GET.get("layout", "cycles")
    layout = layout if layout in {"calendar", "cycles"} else "cycles"
    mode = "full" if request.GET.get("cycle_mode") == "full" else "key"
    selected_metrics = [metric for metric in request.GET.getlist("metrics") if metric in METRICS] or list(METRICS)
    selection = {"cycle_metric_codes": selected_metrics, "include_pending_cycles": request.GET.get("include_pending") == "1"}
    timeline = build_cycle_timeline(data, selection)
    from apps.documents.archive import records_context
    archive = records_context(request.patient, request.GET)
    page_ids = {str(row.pk) for row in archive["page_obj"].object_list}
    filtered_ids = {str(row.pk) for row in archive["page_obj"].paginator.object_list}
    # All original anchors still determine boundaries. Display pagination never
    # makes an omitted cycle disappear from the underlying organization context.
    overlays = build_cycle_overlays(timeline, {**data, "records": [row for row in data["records"] if row["document_id"] in filtered_ids]}, selection)
    for key in ("records", "links", "unassigned"):
        timeline[key] = [row for row in timeline[key] if row["document_id"] in page_ids]
    pagination = request.GET.copy()
    pagination.pop("page", None)
    pagination["patient"] = str(request.patient.pk)
    records = {(row["kind"], row["id"]): row for row in timeline["records"]}
    for link in timeline["links"]:
        link["record"] = records[(link["kind"], link["source_id"])]
    groups = defaultdict(list)
    for row in timeline["cycles"]:
        row["records"] = [link for link in timeline["links"] if link["cycle_id"] == row["id"]]
        groups[row.get("regimen_id")].append(row)
    schemes = {row["id"]: row for row in data["regimens"]}
    calendar = sorted(timeline["links"], key=lambda row: (not row["record"].get("date"), row["record"].get("date") or "", row["source_id"]))
    return _render(request, "treatments/index.html", {"data": data, "timeline": timeline, "overlays": overlays,
        "charts": chart_groups(overlays, mode=mode), "calendar": calendar, "layout": layout, "cycle_mode": mode,
        "groups": [{"regimen": schemes.get(key), "cycles": rows} for key, rows in groups.items()],
        "preview": preview, "proposal_form": ProposalForm(initial={"expected_fingerprint": preview["input_fingerprint"]}, auto_id="batch-proposal-%s"),
        "error_form": error_form, "selection": selection, "metric_options": [(metric, metric in selected_metrics) for metric in METRICS],
        "archive": archive, "pagination_query": pagination.urlencode()}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def event_new(request):
    form = EventForm(request.POST or None, initial={"kind": "SYSTEMIC_TREATMENT", "occurrence": "OCCURRED", "date_precision": "UNKNOWN"})
    for key in ["expected_revision", "expected_sources", "checked_original"]:
        form.fields.pop(key)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["occurred_on"] = data.pop("date") or None
            try:
                event = create_manual_event(request.patient, actor=request.user, **data)
                return redirect("treatments:event", event_id=event.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
        else:
            status = 400
    return _render(request, "treatments/form.html", {"title": "补记治疗或住院事件", "form": form,
        "description": "只记录已知情况。计划、建议和医嘱请选择对应状态；日期或周期序号不明可以留空。", "submit_label": "保存本人补记"}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def event_detail(request, event_id):
    instance, row, material, history = _read(request, "event", event_id)
    form = EventForm(request.POST or None, initial=_initial(row, material, "event"))
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            changes = {key: data[key] for key in ["title", "kind", "occurrence", "date", "date_precision", "regimen_text", "cycle_ordinal", "cycle_day", "note"]}
            changes["date"] = changes["date"] or None
            action = request.POST.get("action")
            try:
                _confirm_unchanged(action, changes, row["content"])
                revise_event(request.patient, instance.pk, actor=request.user, action=action, expected_revision=data["expected_revision"],
                    operation_id=data["operation_id"], checked_original=data["checked_original"], expected_sources=data["expected_sources"],
                    changes=changes if action == "CORRECT" else None)
                return redirect("treatments:event", event_id=instance.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
        else:
            status = 400
    return _render(request, "treatments/detail.html", {"kind": "event", "title": row["content"]["title"], "row": row,
        "form": form, "history": history, "sources": row["sources"]}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def regimen_new(request):
    material = _material(request)
    events = _available(material["events"])
    tokens = {row["id"]: event_token({key: value for key, value in row.items() if key not in {"origin_label", "status_label"}}) for row in events}
    form = RegimenForm(request.POST or None, events=events, initial={"expected_revision": 0, "expected_sources": tokens})
    form.fields.pop("note")
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                expected = data["expected_sources"] or {}
                if not isinstance(expected, dict):
                    raise ValidationError("方案依据标识无效。")
                row = regimens.create_regimen(request.patient, actor=request.user, text=data["text"], event_ids=data["event_ids"],
                    expected_sources={key: expected.get(key) for key in data["event_ids"]}, checked_original=data["checked_original"], operation_id=data["operation_id"])
                return redirect("treatments:regimen", regimen_id=row.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    return _render(request, "treatments/form.html", {"title": "明确记录一个方案", "form": form,
        "description": "相同名称可以是不同治疗阶段。这里只整理来源或本人记录，不推荐或替换用药方案。", "submit_label": "保存方案"}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def regimen_detail(request, regimen_id):
    instance, row, material, history = _read(request, "regimen", regimen_id)
    form = RegimenForm(request.POST or None, initial=_initial(row, material, "regimen"))
    form.fields.pop("event_ids")
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data, action = form.cleaned_data, request.POST.get("action")
            changes = {key: data[key] for key in ["text", "note"]}
            try:
                _confirm_unchanged(action, changes, row["content"])
                regimens.revise_regimen(request.patient, instance.pk, actor=request.user, action=action,
                    expected_revision=data["expected_revision"], operation_id=data["operation_id"], checked_original=data["checked_original"],
                    changes=changes if action == "CORRECT" else None, expected_sources=data["expected_sources"])
                return redirect("treatments:regimen", regimen_id=instance.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
        else:
            status = 400
    events = [event for event in material["events"] if event["id"] in row["content"].get("event_tokens", {})]
    return _render(request, "treatments/detail.html", {"kind": "regimen", "title": row["content"]["text"], "row": row,
        "form": form, "history": history, "events": events, "cycles": [cycle for cycle in material["cycles"] if cycle["regimen_id"] == row["id"]]}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def cycle_new(request):
    material = _material(request)
    events = _available(material["events"])
    tokens = {row["id"]: event_token({key: value for key, value in row.items() if key not in {"origin_label", "status_label"}}) for row in events}
    form = CycleForm(request.POST or None, events=events, regimens=_available(material["regimens"]),
                     initial={"expected_revision": 0, "expected_sources": tokens, "anchor_precision": "UNKNOWN", "end_precision": "UNKNOWN"})
    form.fields["event_ids"].required = True
    for key in ["end", "end_precision", "note"]:
        form.fields.pop(key)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                expected = data["expected_sources"] or {}
                if not isinstance(expected, dict):
                    raise ValidationError("周期来源标识无效。")
                row = cycles.create_cycle(request.patient, actor=request.user, event_ids=data["event_ids"],
                    expected_sources={key: expected.get(key) for key in data["event_ids"]}, anchor=data["anchor"], anchor_precision=data["anchor_precision"],
                    ordinal=data["ordinal"], regimen_id=data["regimen_id"] or None, checked_original=data["checked_original"], operation_id=data["operation_id"])
                return redirect("treatments:cycle", cycle_id=row.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    return _render(request, "treatments/form.html", {"title": "明确组织一个周期", "form": form,
        "description": "自动提议在治疗首页核对。这里用于明确的人工组织；显示区间不是治疗持续时间，序号不明请留空。", "submit_label": "保存周期"}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def cycle_detail(request, cycle_id):
    instance, row, material, history = _read(request, "cycle", cycle_id)
    form = CycleForm(request.POST or None, initial=_initial(row, material, "cycle"), regimens=_available(material["regimens"]))
    form.fields.pop("event_ids")
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data, action = form.cleaned_data, request.POST.get("action")
            changes = {key: data[key] for key in ["anchor", "anchor_precision", "ordinal", "end", "end_precision", "note", "regimen_id"]}
            changes["regimen_id"] = changes["regimen_id"] or None
            try:
                _confirm_unchanged(action, changes, {**row["content"], "regimen_id": row["regimen_id"]})
                cycles.revise_cycle(request.patient, instance.pk, actor=request.user, action=action,
                    expected_revision=data["expected_revision"], operation_id=data["operation_id"], checked_original=data["checked_original"],
                    expected_sources=data["expected_sources"], changes=changes if action == "CORRECT" else None)
                return redirect("treatments:cycle", cycle_id=instance.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    events = [event for event in material["events"] if event["id"] in {link["event_id"] for link in row["event_links"]}]
    return _render(request, "treatments/detail.html", {"kind": "cycle", "title": "周期核对", "row": row, "form": form, "history": history,
        "events": events, "record_links": [link for link in material["record_associations"] if link["cycle_id"] == row["id"]],
        "lineage": [link for link in material["lineage"] if row["id"] in {link["predecessor_id"], link["successor_id"]}]}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def merge(request):
    material = _material(request)
    current = _available(material["cycles"])
    form = MergeForm(request.POST or None, cycles=current, regimens=_available(material["regimens"]),
        initial={"expected_revision": 0, "expected_revisions": {row["id"]: row["revision_number"] for row in current},
                 "anchor_precision": "UNKNOWN", "end_precision": "UNKNOWN"})
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                expected = data["expected_revisions"]
                if not isinstance(expected, dict):
                    raise ValidationError("当前周期版本标识无效。")
                resolution = {key: data[key] for key in ["anchor", "anchor_precision", "ordinal", "end", "end_precision", "note", "regimen_id"]}
                resolution["regimen_id"] = resolution["regimen_id"] or None
                row = cycles.merge_cycles(request.patient, actor=request.user, cycle_ids=data["cycle_ids"],
                    expected_revisions={key: expected.get(key) for key in data["cycle_ids"]}, resolution=resolution,
                    checked_original=data["checked_original"], operation_id=data["operation_id"])
                return redirect("treatments:cycle", cycle_id=row.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    return _render(request, "treatments/form.html", {"title": "合并周期", "form": form,
        "description": "请明确填写合并后的日期、序号与方案；不明项保留未知。原周期及每次更正都会保留，不取平均日期。", "submit_label": "核对并合并"}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def split(request, cycle_id):
    instance, row, material, history = _read(request, "cycle", cycle_id)
    events = [event for event in material["events"] if event["id"] in {link["event_id"] for link in row["event_links"]}]
    links = [link for link in material["record_associations"] if link["cycle_id"] == row["id"]]
    try:
        count = min(50, max(2, int(request.GET.get("parts", "2"))))
    except ValueError:
        count = 2
    Parts = formset_factory(SplitPartForm, extra=0, min_num=2, max_num=50, validate_min=True, validate_max=True, absolute_max=50)
    parts = Parts(request.POST or None, prefix="parts", initial=[{} for _ in range(count)],
                  form_kwargs={"events": events, "regimens": _available(material["regimens"]), "record_links": links})
    for part in parts:
        part.initial.update({"anchor_precision": "UNKNOWN", "end_precision": "UNKNOWN"})
    form = DecisionForm(request.POST or None, initial=_initial(row, material, "cycle"))
    status = 200
    if request.method == "POST":
        if form.is_valid() and parts.is_valid():
            data = form.cleaned_data
            try:
                values = [{key: value for key, value in part.cleaned_data.items() if key in cycles.CYCLE_FIELDS | {"event_ids", "record_link_ids", "regimen_id"}} for part in parts if part.cleaned_data]
                children = cycles.split_cycle(request.patient, instance.pk, actor=request.user, expected_revision=data["expected_revision"],
                    parts=values, checked_original=data["checked_original"], operation_id=data["operation_id"])
                return redirect("treatments:cycle", cycle_id=children[0].pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Treatment not found") from None
        else:
            status = 400
    return _render(request, "treatments/split.html", {"row": row, "form": form, "parts": parts, "part_count": count}, status)


@patient_required
@require_http_methods(["GET", "POST"])
def assign(request, cycle_id):
    instance, row, material, history = _read(request, "cycle", cycle_id, full=True)
    records = material["records"]
    # The browser selects a source first; one source token is checked when it saves.
    key = request.POST.get("record_key") if request.method == "POST" else request.GET.get("record_key")
    key = key or (f'{records[0]["kind"]}:{records[0]["id"]}' if records else "")
    selected = next((record for record in records if f'{record["kind"]}:{record["id"]}' == key), None)
    current = record_state(request.patient, actor=request.user, kind=selected["kind"], identity=selected["id"]) if selected else None
    initial = {**_initial(row, material, "cycle"), "record_key": key, "expected_record_sources": {key: current["source_token"]} if current else {},
               "expected_assignments": {key: current["assignment_token"]} if current else {}, "assigned": "true"}
    form = AssignmentForm(request.POST or None, records=records, initial=initial)
    form.fields["record_key"].widget = HiddenInput()
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                if not isinstance(data["expected_record_sources"], dict) or not isinstance(data["expected_assignments"], dict):
                    raise ValidationError("来源归属标识无效。")
                kind, identity = data["record_key"].split(":", 1)
                assign_record(request.patient, instance.pk, actor=request.user, kind=kind, identity=identity,
                    expected_source=data["expected_record_sources"].get(data["record_key"]), expected_assignment=data["expected_assignments"].get(data["record_key"]),
                    expected_revision=data["expected_revision"], checked_original=data["checked_original"], operation_id=data["operation_id"], assigned=data["assigned"] == "true")
                return redirect("treatments:cycle", cycle_id=instance.pk)
            except (ValidationError, TreatmentConflict) as exc:
                status = _error(form, exc)
            except PermissionDenied:
                raise Http404("Source not found") from None
        else:
            status = 400
    return _render(request, "treatments/assign.html", {"row": row, "form": form, "records": records, "selected": selected, "selected_key": key}, status)
