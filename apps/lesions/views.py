from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.patients.access import Capability

from .forms import CreateForm, ManageForm, MatchForm, NameForm, ProposalForm, SelectPairForm
from .models import Lesion, LesionMatchProposal, LesionOperation
from .presentation import display_observation, display_operation, display_proposal, display_trends
from .readmodels import lesion_state, observation_material, proposal_material
from .services import (create_lesion, decide_proposal, generate_proposals, match_observations,
                       rename_lesion, split_observations, undo_operation, unlink_observations)
from .trends import build_trends


def _render(request, template, context, *, status=200):
    return protect_sensitive_html(render(request, template, {
        "current_section": "records", "can_write": request.patient_access.permits(Capability.WRITE),
        **context,
    }, status=status))


def _redirect(request, name, *args):
    return redirect(reverse(name, args=args) + "?patient=" + str(request.patient.pk))


@patient_required
@require_safe
def index(request):
    material = observation_material(request.patient, include_unavailable=True)
    observations = [display_observation(row) for row in material]
    proposals = [display_proposal(row) for row in proposal_material(request.patient, observations=material, include_history=True)]
    lookup = {row["id"]: row for row in observations}
    for proposal in proposals:
        proposal["first"] = lookup.get(proposal["first_id"])
        proposal["second"] = lookup.get(proposal["second_id"])
    return _render(request, "lesions/index.html", {
        "observations": observations, "proposals": proposals,
        "lesions": [lesion_state(lesion) for lesion in Lesion.objects.filter(patient=request.patient)],
        "pair_form": SelectPairForm(observations=observations),
    })


@patient_required(capability=Capability.WRITE)
@require_POST
def generate(request):
    generate_proposals(request.patient, actor=request.user)
    return _redirect(request, "lesions:index")


def _matching_initial(request, rows):
    lesions = [lesion for lesion in Lesion.objects.filter(patient=request.patient,
                pk__in={row["lesion_id"] for row in rows if row["lesion_id"]}).order_by("pk")]
    targets = [(str(lesion.pk), lesion_state(lesion)["name"] + " · " + str(lesion.pk)) for lesion in lesions]
    if len(targets) > 1:
        targets.insert(0, ("", "请选择明确保留的标识"))
    initial = {"expectations": {row["id"]: {"revision": row["revision_number"], "source": row["source_token"]} for row in rows},
               "expected_lesion_revisions": {str(lesion.pk): lesion.revision_number for lesion in lesions},
               "target": targets[0][0] if targets else "NEW"}
    return targets, initial


def _proposal_context(request, proposal):
    material = observation_material(request.patient, include_unavailable=True)
    state = next((row for row in proposal_material(request.patient, observations=material, include_history=True)
                  if row["id"] == str(proposal.pk)), None)
    lookup = {row["id"]: row for row in material}
    rows = [lookup.get(str(identity)) for identity in (proposal.first_id, proposal.second_id)]
    if state is None or any(row is None for row in rows):
        raise Http404("观察已不可用。")
    targets, initial = _matching_initial(request, rows)
    initial.update(expected_revision=proposal.revision_number, expected_fingerprint=proposal.fingerprint)
    return rows, state, targets, initial


@patient_required
@require_http_methods(["GET", "HEAD", "POST"])
def proposal(request, lesion_proposal_id):
    record = get_object_or_404(LesionMatchProposal.objects.filter(patient=request.patient), pk=lesion_proposal_id)
    if request.method == "POST" and not request.patient_access.permits(Capability.WRITE):
        raise PermissionDenied
    rows, state, targets, initial = _proposal_context(request, record)
    action = request.POST.get("action", "") if request.method == "POST" else "CONFIRM"
    form = ProposalForm(request.POST if request.method == "POST" else None, initial=initial, targets=targets, action=action)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                operation = decide_proposal(request.patient, actor=request.user, proposal_id=record.pk,
                                             action=action, **form.arguments())
            except ValidationError as error:
                form.add_error(None, error)
                status = 409
            else:
                effect = operation.lesion_revisions.first()
                return _redirect(request, "lesions:detail", effect.lesion_id) if effect else _redirect(request, "lesions:proposal", record.pk)
    return _render(request, "lesions/proposal.html", {
        "proposal": display_proposal(state), "observations": [display_observation(row) for row in rows], "form": form,
        "history": [display_operation(revision.operation) for revision in record.revisions.select_related("operation").order_by("-sequence")],
        "may_decide": state["source_current"] and state["decision"] in {"PENDING", "REJECTED", "DEFERRED"},
    }, status=status)


@patient_required
@require_safe
def detail(request, lesion_id):
    record = get_object_or_404(Lesion.objects.filter(patient=request.patient), pk=lesion_id)
    rows = [row for row in observation_material(request.patient, include_unavailable=True) if row["lesion_id"] == str(record.pk)]
    return _render(request, "lesions/detail.html", {
        "lesion": lesion_state(record), "observations": [display_observation(row) for row in rows],
        **display_trends(build_trends(rows)),
        "history": [display_operation(revision.operation) for revision in record.revisions.select_related("operation").order_by("-sequence")],
        "rename_form": NameForm(initial={"expected_revision": record.revision_number, "name": lesion_state(record)["name"]}),
    })


@patient_required
@require_safe
def maximum(request):
    rows = [row for row in observation_material(request.patient, include_unavailable=True) if row["lesion_id"]]
    return _render(request, "lesions/maximum.html", display_trends(build_trends(rows, report_maximum=True)))


@patient_required
@require_http_methods(["GET", "HEAD", "POST"])
def observation(request, report_id, entity_key):
    if request.method == "POST" and not request.patient_access.permits(Capability.WRITE):
        raise PermissionDenied
    row = next((row for row in observation_material(request.patient, include_unavailable=True)
                if row["report_id"] == str(report_id) and row["entity_key"] == entity_key), None)
    if row is None:
        raise Http404("观察不属于当前患者或已不可用。")
    form = CreateForm(request.POST if request.method == "POST" else None,
                       initial={"expected_revision": row["revision_number"], "expected_source": row["source_token"]})
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                operation = create_lesion(request.patient, actor=request.user, observation_id=row["id"], **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
                status = 409
            else:
                return _redirect(request, "lesions:detail", operation.lesion_revisions.get().lesion_id)
    return _render(request, "lesions/observation.html", {"observation": display_observation(row), "form": form}, status=status)


@patient_required
@require_http_methods(["GET", "HEAD", "POST"])
def match(request):
    if request.method == "POST" and not request.patient_access.permits(Capability.WRITE):
        raise PermissionDenied
    material = observation_material(request.patient, include_unavailable=True)
    selection_data = request.POST if request.method == "POST" else (
        request.GET if any(key in request.GET for key in ("first_id", "second_id")) else None)
    selection = SelectPairForm(selection_data,
                               observations=[display_observation(row) for row in material])
    if not selection.is_bound or not selection.is_valid():
        return _render(request, "lesions/select.html", {"form": selection}, status=400 if selection.is_bound else 200)
    lookup = {row["id"]: row for row in material}
    rows = [lookup[selection.cleaned_data[key]] for key in ("first_id", "second_id")]
    targets, initial = _matching_initial(request, rows)
    initial.update(first_id=rows[0]["id"], second_id=rows[1]["id"])
    form = MatchForm(request.POST if request.method == "POST" else None, initial=initial, targets=targets)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                operation = match_observations(request.patient, actor=request.user, **form.arguments())
            except ValidationError as error:
                form.add_error(None, error)
                status = 409
            else:
                return _redirect(request, "lesions:detail", operation.observation_revisions.first().lesion_id)
    return _render(request, "lesions/match.html", {"observations": [display_observation(row) for row in rows],
                                                 "form": form}, status=status)


@patient_required(capability=Capability.WRITE)
@require_POST
def rename(request, lesion_id):
    record = get_object_or_404(Lesion.objects.filter(patient=request.patient), pk=lesion_id)
    form = NameForm(request.POST)
    status = 400
    if form.is_valid():
        try:
            rename_lesion(request.patient, actor=request.user, lesion_id=record.pk, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
            status = 409
        else:
            return _redirect(request, "lesions:detail", record.pk)
    return _render(request, "lesions/error.html", {"form": form}, status=status)


@patient_required(capability=Capability.WRITE)
@require_http_methods(["GET", "HEAD", "POST"])
def manage(request, lesion_id):
    record = get_object_or_404(Lesion.objects.filter(patient=request.patient), pk=lesion_id)
    rows = [display_observation(row) for row in observation_material(request.patient, include_unavailable=True)
            if row["lesion_id"] == str(record.pk)]
    action = request.POST.get("action", "") if request.method == "POST" else "SPLIT"
    form = ManageForm(request.POST if request.method == "POST" else None, observations=rows, action=action,
        initial={"expected_revision": record.revision_number,
                 "expectations": {row["id"]: {"revision": row["revision_number"], "source": row["source_token"]} for row in rows}})
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                if action == "SPLIT":
                    split_observations(request.patient, actor=request.user, lesion_id=record.pk, **form.arguments(),
                                       name=form.cleaned_data["name"], checked_original=form.cleaned_data["checked_original"])
                else:
                    unlink_observations(request.patient, actor=request.user, lesion_id=record.pk, **form.arguments())
            except ValidationError as error:
                form.add_error(None, error)
                status = 409
            else:
                return _redirect(request, "lesions:detail", record.pk)
    return _render(request, "lesions/manage.html", {"lesion": lesion_state(record), "observations": rows, "form": form}, status=status)


@patient_required
@require_safe
def operation(request, lesion_operation_id):
    record = get_object_or_404(LesionOperation.objects.filter(patient=request.patient), pk=lesion_operation_id)
    effects = []
    for revision in record.lesion_revisions.all():
        effects.append({"kind": "观察名称及标识", "identity": str(revision.lesion_id),
                        "before": revision.before["name"], "after": revision.after["name"],
                        "before_active": revision.before["active"], "after_active": revision.after["active"]})
    for revision in record.observation_revisions.select_related("observation"):
        effects.append({"kind": "报告观察的关联", "identity": str(revision.observation_id),
                        "before": revision.before["lesion_id"] or "未关联", "after": revision.after["lesion_id"] or "未关联",
                        "report_id": str(revision.observation.original_report_id), "entity_key": revision.observation.entity_key})
    for revision in record.proposal_revisions.all():
        from .presentation import STATUS_LABELS
        effects.append({"kind": "提议核对状态", "identity": str(revision.proposal_id),
                        "before": STATUS_LABELS[revision.before["status"]], "after": STATUS_LABELS[revision.after["status"]]})
    return _render(request, "lesions/operation.html", {"operation": display_operation(record), "effects": effects})


@patient_required(capability=Capability.WRITE)
@require_POST
def undo(request, lesion_operation_id):
    record = get_object_or_404(LesionOperation.objects.filter(patient=request.patient), pk=lesion_operation_id)
    try:
        undo_operation(request.patient, actor=request.user, operation_id=record.pk)
    except ValidationError as error:
        return _render(request, "lesions/error.html", {"error": "；".join(error.messages)}, status=409)
    return _redirect(request, "lesions:operation", record.pk)
