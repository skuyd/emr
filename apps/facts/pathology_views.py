"""Review original text separately from immutable specimen/assay associations."""
import uuid

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect

from apps.core.responses import protect_sensitive_html
from apps.patients.access import Capability

from .clinical_forms import BoundaryReplacementForm, ReportActionForm
from .clinical_readmodels import report_material, report_queryset, review_reports
from .clinical_schema import FIELDS
from .clinical_services import add_manual_clinical_field, replace_report_boundary, revise_report
from .pathology_forms import ContextActionForm, PathologyRevisionForm, PathologySourceForm, ROLE_LABELS
from .pathology_services import affected_context_fields, revise_context_group
from .readmodels import digest, effective_fact, fact_queryset
from .revisions import FactConflict, revise_fact


def _source_form(key):
    from .molecular_schema import SCHEMA
    if FIELDS[key].version == SCHEMA:
        from .molecular_source_forms import MolecularSourceForm
        return MolecularSourceForm
    return PathologySourceForm


def _revision_form(key):
    from .molecular_schema import SCHEMA
    if FIELDS[key].version == SCHEMA:
        from .molecular_forms import MolecularRevisionForm
        return MolecularRevisionForm
    return PathologyRevisionForm


def history_rows(query):
    return list(query.select_related("author").order_by("-sequence"))


def history_identity(history):
    return [(str(row.pk), row.sequence, str(row.author_id) if row.author_id else None, digest(row.before), digest(row.after)) for row in history]


def render_current(request, template, context, *, material, reread, status=200):
    from .views import _render, assert_render_access

    before = digest(material)
    response = _render(request, template, context, status=status)
    # Compare the rows that actually went into the response. A fresh second
    # pre-render query could instead validate a changed head against stale HTML.
    after = digest(reread())
    # The final source query can outlive the earlier render authorization.
    # Recheck even for invalid POST responses that still contain private forms.
    assert_render_access(request, context)
    if before != after:
        return protect_sensitive_html(HttpResponse("来源、关联或作者记录已变化，请刷新后重新核对。", status=410))
    return response


def _error(exc):
    return ("；".join(exc.messages), 400) if isinstance(exc, ValidationError) else (str(exc), 409)


def _anchors(report):
    keys = ("specimen.identity", "assay.identity", "ihc.marker")
    if report.routing_kind == "MOLECULAR":
        keys += ("variant.identity", "drug_evidence.drugs")
    return list(report.fields.filter(field_key__in=keys)
                .select_related("document_page").order_by("reading_order", "pk"))


def _entity(key, context):
    kind = FIELDS[key].entity_kind
    if kind == "report":
        return "report"
    role = {"specimen": "SPECIMEN", "assay": "ASSAY", "ihc": "MARKER", "variant": "VARIANT", "drug_evidence": "DRUG_EVIDENCE"}[kind]
    binding = next((item for item in context["bindings"] if item["role"] == role and item["state"] == "BOUND"), None)
    return binding["target_entity_key"] if binding else kind + ":" + uuid.uuid4().hex


def report_detail(request, report):
    rows = review_reports(request.patient, actor=request.user, report_ids=[report.pk], include_history=True)
    if not rows:
        raise Http404
    row = rows[0]
    key = request.POST.get("field_key") or request.GET.get("field_key", "specimen.identity")
    from .molecular_schema import field_allowed_in_report
    allowed = lambda k: field_allowed_in_report(k, "MOLECULAR") if report.routing_kind == "MOLECULAR" else FIELDS[k].category == "PATHOLOGY"
    if key not in FIELDS or not allowed(key):
        raise Http404
    anchors = _anchors(report)
    form = _source_form(key)(key, report, anchors=anchors, initial={"expected_report_source": row["current_source_token"],
                                                                    "page_number": row["pages"][0], "source_role": "CURRENT_RESULT"})
    initial = {"expected_revision": report.revision_number, "expected_source": row["current_source_token"]}
    action_form = ReportActionForm(initial=initial)
    boundary_form = BoundaryReplacementForm(report, initial={**initial, "title": report.title, "mode": "pages",
                                                            "first_page": row["pages"][0], "last_page": row["pages"][-1]})
    error, status = "", 200
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "REPLACE":
                boundary_form = BoundaryReplacementForm(report, request.POST)
                if boundary_form.is_valid():
                    values = boundary_form.cleaned_data
                    replacement = replace_report_boundary(request.patient, actor=request.user, report_id=report.pk,
                                                          title=values["title"], spans=values["spans"], expected_revision=values["expected_revision"], expected_source=values["expected_source"])
                    return redirect("facts:report", report_id=replacement.pk)
            elif action in {"EXCLUDE", "UNDO"}:
                action_form = ReportActionForm(request.POST)
                if action_form.is_valid():
                    revise_report(request.patient, actor=request.user, report_id=report.pk, action=action, **action_form.cleaned_data)
                    return redirect("facts:report", report_id=report.pk)
            elif action == "manual_field":
                form = _source_form(key)(key, report, request.POST, anchors=anchors)
                if form.is_valid():
                    values = form.cleaned_data
                    field = add_manual_clinical_field(request.patient, actor=request.user, report_id=report.pk, field_key=key,
                                                      entity_key=_entity(key, values["entity_context"]), value=values["value"],
                                                      fragments=values["fragments"], entity_context=values["entity_context"],
                                                      source_role=values["source_role"], expected_report_source=values["expected_report_source"],
                                                      **({"reported_assertion": values["reported_assertion"], "own_fragment_count": values["own_fragment_count"]} if "reported_assertion" in values else {}))
                    return redirect("facts:detail", fact_id=field.pk)
            else:
                raise ValidationError("请选择当前页面提供的操作。")
            status = 400
        except (ValidationError, FactConflict) as exc:
            error, status = _error(exc)
    history = history_rows(report.revisions)

    def reread():
        fresh = report_queryset().get(pk=report.pk)
        return {"row": review_reports(request.patient, actor=request.user, report_ids=[report.pk], include_history=True)[0],
                "history": history_identity(history_rows(fresh.revisions))}

    return render_current(request, "facts/report.html", {"report": report, "row": row, "form": form, "action_form": action_form,
                          "boundary_form": boundary_form, "field_keys": [(k, spec.label) for k, spec in FIELDS.items() if allowed(k)],
                          "field_key": key, "is_pathology": report.routing_kind == "PATHOLOGY", "is_molecular": report.routing_kind == "MOLECULAR",
                          "error": error, "can_write": request.patient_access.permits(Capability.WRITE), "history": history},
                          material={"row": row, "history": history_identity(history)}, reread=reread, status=status)


def _context_forms(request, fact, row, *, bound):
    report = fact.clinical_report
    anchors = _anchors(report)
    all_fields = list(report.fields.select_related("document_page"))
    entries = []
    for field in affected_context_fields(fact, all_fields):
        current = effective_fact(field)
        fragments = current["fragments"]
        same_page = len({fragment["page"] for fragment in fragments}) == 1
        initial = {"raw_value": "\n".join(fragment["raw_text"] for fragment in fragments) if same_page else "",
                   "page_number": fragments[0]["page"] if same_page else "",
                   "expected_report_source": report_material_source(report),
                   "source_role": current["content"]["source_role"]}
        extra = {}
        from .molecular_schema import SCHEMA
        if field.schema_version == SCHEMA:
            extra = {"association": field.automatic_content["entity_context"]["association"], "reported_assertion": current["content"]["reported_assertion"]}
            own = field.automatic_content.get("literal_source", {}).get("value_fragment_ordinals", list(range(len(fragments))))
            if "manual_source" in field.automatic_content:
                own = list(range(field.automatic_content["manual_source"]["own_fragment_count"]))
            own_fragments = [fragments[i] for i in own if i < len(fragments)]
            same_page = len({p["page"] for p in own_fragments}) == 1
            initial.update(raw_value="\n".join(p["raw_text"] for p in own_fragments) if same_page else "",
                           page_number=own_fragments[0]["page"] if same_page else "")
            if own_fragments and not same_page:
                initial.update(raw_value=own_fragments[0]["raw_text"], page_number=own_fragments[0]["page"], supplemental_count=len(own_fragments)-1)
                for index, piece in enumerate(own_fragments[1:]):
                    initial.update({f"supplemental_{index}_page": piece["page"], f"supplemental_{index}_text": piece["raw_text"]})
        form = _source_form(field.field_key)(field.field_key, report, request.POST if bound else None, value=current["content"]["value"],
                                   anchors=anchors, bindings=field.automatic_content["entity_context"]["bindings"], fragments=fragments,
                                   prefix="replace-" + str(field.pk), initial=initial, **extra)
        # Source role is immutable for this group operation. A different role
        # requires a separately authored candidate, not hidden reassignment.
        form.fields["source_role"].disabled = True
        entries.append({"field": field, "row": current, "form": form})
    return entries


def report_material_source(report):
    from .clinical_readmodels import report_source_token
    return report_source_token(report)


def field_detail(request, fact):
    row = effective_fact(fact)
    initial = {"raw_value": row["content"]["raw_value"], "expected_revision": fact.revision_number, "expected_source": row["current_source_token"]}
    form = _revision_form(fact.field_key)(fact.field_key, value=row["content"]["value"], initial=initial)
    group_allowed = FIELDS[fact.field_key].entity_kind != "report"
    context_initial = {**initial, "expected_material": digest(report_material(request.patient, report_ids=[fact.clinical_report_id]))}
    context_action_form = ContextActionForm(initial=context_initial)
    action = request.POST.get("action") if request.method == "POST" else None
    edit_context = bool(request.GET.get("edit_context") == "1" or action == "REPLACE_CONTEXT") and group_allowed
    replacements = _context_forms(request, fact, row, bound=action == "REPLACE_CONTEXT") if edit_context else []
    error, status = "", 200
    if request.method == "POST":
        try:
            if action in {"REPLACE_CONTEXT", "UNDO_CONTEXT"}:
                if not group_allowed:
                    raise ValidationError("本字段没有可替换的检测或标本关联。")
                context_action_form = ContextActionForm(request.POST)
                valid = context_action_form.is_valid()
                for entry in replacements:
                    valid = entry["form"].is_valid() and valid
                if valid:
                    values = context_action_form.cleaned_data
                    changes = {"expected_material": values["expected_material"]}
                    if action == "REPLACE_CONTEXT":
                        changes["replacements"] = [{"old_fact_id": str(entry["field"].pk), "value": entry["form"].cleaned_data["value"],
                                                    "fragments": entry["form"].cleaned_data["fragments"],
                                                    "bindings": entry["form"].cleaned_data["entity_context"]["bindings"],
                                                    **({"association": entry["form"].cleaned_data["entity_context"]["association"], "reported_assertion": entry["form"].cleaned_data["reported_assertion"], "own_fragment_count": entry["form"].cleaned_data["own_fragment_count"]}
                                                       if "reported_assertion" in entry["form"].cleaned_data else {})} for entry in replacements]
                    revise_context_group(request.patient, fact.pk, actor=request.user, action=action, expected_revision=values["expected_revision"],
                                         expected_source=values["expected_source"], changes=changes, checked_original=values["checked_original"])
                    return redirect("facts:detail", fact_id=fact.pk)
            else:
                form = _revision_form(fact.field_key)(fact.field_key, request.POST, value=row["content"]["value"], initial=initial)
                if form.is_valid():
                    values = form.cleaned_data
                    if action == "CONFIRM" and (values["value"] != row["content"]["value"] or values["raw_value"] != row["content"]["raw_value"]):
                        raise ValidationError("字段已有修改，请使用“保存更正并确认”。")
                    revise_fact(request.patient, fact.pk, actor=request.user, action=action, expected_revision=values["expected_revision"],
                                expected_source=values["expected_source"], checked_original=values["checked_original"],
                                changes={"value": values["value"], "raw_value": values["raw_value"]} if action == "CORRECT" else None)
                    return redirect("facts:detail", fact_id=fact.pk)
            status = 400
        except (ValidationError, FactConflict) as exc:
            error, status = _error(exc)
    history = history_rows(fact.revisions)
    linked = []
    for binding in row["content"]["entity_context"]["bindings"]:
        target = next((head for head in row["context_snapshot"]["dependency_heads"] if head["fact_id"] == binding["target_fact_id"]), None)
        linked.append({"role": {"SPECIMEN": "标本", "ASSAY": "检测", "MARKER": "标记", "VARIANT": "完整变异身份", "DRUG_EVIDENCE": "药物依据组"}[binding["role"]],
                       "id": binding["target_fact_id"], "state": "已核对" if target and target["usable"] else "待核对" if target else "未关联"})

    def reread():
        fresh = fact_queryset().get(pk=fact.pk)
        return {"row": effective_fact(fresh), "history": history_identity(history_rows(fresh.revisions)),
                "material": digest(report_material(request.patient, report_ids=[fact.clinical_report_id]))}

    return render_current(request, "facts/pathology-field.html", {"fact": fact, "row": row, "form": form, "error": error,
                          "can_write": request.patient_access.permits(Capability.WRITE), "history": history,
                          "context_action_form": context_action_form, "replacement_forms": replacements, "edit_context": edit_context,
                          "group_allowed": group_allowed, "bindings": linked, "is_molecular": fact.clinical_report.routing_kind == "MOLECULAR",
                          "source_role_label": {**ROLE_LABELS, "REPORT_DRUG_EVIDENCE": "报告药物依据"}[row["content"]["source_role"]]},
                          material={"row": row, "history": history_identity(history), "material": context_initial["expected_material"]}, reread=reread, status=status)
