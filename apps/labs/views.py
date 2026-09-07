"""Private owner, task-scoped reviewer and dictionary-manager interfaces."""

from contextlib import closing
from datetime import datetime
from functools import wraps
import json

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.decorators import patient_required
from apps.patients.access import authorize_patient, accessible_patients
from apps.core.responses import protect_sensitive_html
from apps.documents.backends import get_object_store
from apps.documents.errors import UploadDomainError
from apps.documents.locking import lock_document_aggregate
from apps.documents.previews import PreviewUnavailable, render_page
from apps.documents.views.originals import _highlight_rect, _protect_page_image
from apps.operations.models import DictionaryRelease
from apps.operations.audit import record_audit_event
from apps.operations.permissions import Action, Role, authorize
from apps.processing.models import ParsingVersion

from . import dictionary_workflow as workflow
from .comparison import comparison_view
from .dictionary import current_dictionary
from .models import DictionaryCandidate, ObservationRevision, ReviewTask, RevisionAction
from .presentation import CATEGORY_LABELS, REVISION_FEEDBACK, explain_issues, review_status
from .readmodels import checked_reference, effective_rows, observation_queryset
from .review import _is_reviewer, assign_review_task, create_review_task, get_review_task, transition_review_task
from .revisions import EDITABLE_FIELDS, VALUE_FIELDS, RevisionConflict, effective_observation, revise_observation
from .validation import REVIEWABLE_ISSUES, validate_observation


def _render(request, template, context=None, *, status=200, embeddable=False):
    return protect_sensitive_html(render(request, template, context or {}, status=status), embeddable=embeddable)


def workflow_errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except RevisionConflict as error:
            return _render(request, "labs/error.html", {"error": str(error)}, status=409)
        except (ValidationError, workflow.DictionaryWorkflowError, ValueError, TypeError) as error:
            message = "；".join(error.messages) if isinstance(error, ValidationError) else str(error) if isinstance(error, workflow.DictionaryWorkflowError) else "输入无效，请检查内容后重试。"
            return _render(request, "labs/error.html", {"error": message}, status=400)
        except PermissionDenied:
            return _render(request, "labs/error.html", {"error": "无权访问，授权可能已撤回或需要重新验证身份。"}, status=403)
    return wrapped


def _expected(request):
    return int(request.POST.get("expected_revision", ""))


def _changes(request):
    if request.POST.get("correction_mode") == "selected_fields":
        fields = request.POST.getlist("change_fields")
        if not fields or not set(fields) <= EDITABLE_FIELDS or any(field not in request.POST for field in fields):
            raise ValidationError("请选择需要更正的字段并填写内容。")
        return {field: request.POST[field] for field in fields}
    return {field: request.POST[field] for field in EDITABLE_FIELDS if field in request.POST}


def _owner_row(request, observation_id):
    authorize_patient(request.patient, request.user, "write" if request.method == "POST" else "read")
    return get_object_or_404(observation_queryset(), pk=observation_id,
                            parsing_version__document__patient=request.patient,
                            parsing_version__document__patient__account__is_active=True,
                            parsing_version__document__deleted_at__isnull=True)


def _observation_context(row, *, include_patient_context=False):
    effective = effective_observation(row)
    effective.automatic_standard_code = row.standard_code
    effective.automatic_standard_name = row.standard_name
    identities = {source["observation_id"] for source in effective.value_sources.values()} | {str(row.pk)}
    previous = effective_rows(row.parsing_version.document.patient, include_uncertain=True) if include_patient_context else ()
    issues = validate_observation(effective, previous=previous)
    return {"observation": effective, "issues": explain_issues(issues), "review_status": review_status(effective),
            "reference": checked_reference(effective, issues), "revision_actions": RevisionAction.choices,
            "history": ObservationRevision.objects.filter(observation_id__in=identities,
                observation__parsing_version__document_id=row.parsing_version.document_id).select_related("author", "source_evidence").order_by("-created_at", "-sequence"),
            "field_names": (("raw_name", "项目"), ("raw_value", "结果"), ("raw_unit", "单位"),
                            ("observation_date", "日期"), ("reference_range_raw", "参考范围")),
            "correction_fields": tuple({"name": name, "label": label,
                "value": getattr(effective, name).isoformat() if name == "observation_date" and effective.observation_date else getattr(effective, name) or "",
                "input_type": "date" if name == "observation_date" else "text",
                "max_length": row._meta.get_field(name).max_length or 10}
                for name, label in (("raw_name", "项目原文"), ("standard_code", "标准项目编码"), ("raw_value", "结果"), ("raw_unit", "单位"), ("observation_date", "日期")))}


@patient_required
@require_GET
@workflow_errors
def comparison(request):
    start, end = (parse_date(request.GET.get(key, "")) for key in ("start", "end"))
    category, project = (request.GET.get(key, "").strip()[:100] for key in ("category", "project"))
    response = _render(request, "labs/comparison.html", {
        "comparison": comparison_view(request.patient, start=start, end=end, category=category, project=project),
        "start": start, "end": end, "category": category, "project": project,
        "categories": tuple((code, CATEGORY_LABELS.get(code, code)) for code in sorted({item.category for item in current_dictionary().indicators})),
        "current_section": "comparison",
    })
    authorize_patient(request.patient, request.user, "read")
    return response


@patient_required
@require_http_methods(["GET", "POST"])
@workflow_errors
def observation(request, observation_id):
    row = _owner_row(request, observation_id)
    if request.method == "POST":
        event = revise_observation(request.user, row.pk, action=request.POST.get("action"),
                                   changes=_changes(request), expected_revision=_expected(request))
        messages.success(request, REVISION_FEEDBACK[event.action])
        return redirect("labs:observation", row.pk)
    context = _observation_context(row, include_patient_context=True)
    context["review_tasks"] = row.review_tasks.select_related("reviewer").all()
    context["current_section"] = "records"
    return _render(request, "labs/observation.html", context)


@patient_required
@require_POST
@workflow_errors
def create_task(request, observation_id):
    row = _owner_row(request, observation_id)
    reviewer = get_object_or_404(get_user_model(), pk=request.POST["reviewer"]) if request.POST.get("reviewer") else None
    task = create_review_task(request.user, row.pk, reviewer=reviewer)
    return redirect("labs:review_task", task.pk)


@login_required
@require_GET
@workflow_errors
def review_queue(request):
    tasks = ReviewTask.objects.filter(Q(granted_by=request.user) | Q(reviewer=request.user)).order_by("-created_at")
    visible = []
    for identity in tasks.values_list("pk", flat=True):
        try:
            visible.append(get_review_task(request.user, identity))
        except (PermissionDenied, RevisionConflict):
            continue
    response = _render(request, "labs/reviews.html", {"tasks": visible})
    return _review_read_response(request, response, visible, audit_tasks=True)


def _review_read_response(request, response, tasks, *, audit_tasks=False):
    def audit(task, result):
        record_audit_event(request.user.pk, "review_viewed", task.pk, result,
                           patient_id=task.observation.parsing_version.document.patient_id,
                           resource_type="review")
    for task in tasks:
        try:
            # Refresh the professional actor and the exact task grant after
            # rendering. An active family selection cannot authorize this read.
            get_review_task(request.user, task.pk)
        except (PermissionDenied, RevisionConflict):
            response.close()
            if audit_tasks:
                audit(task, "denied")
            raise
    if audit_tasks:
        for task in tasks:
            audit(task, "succeeded")
    return response


@login_required
@require_http_methods(["GET", "POST"])
@workflow_errors
def review_task(request, task_id):
    task = get_review_task(request.user, task_id)
    try:
        access = authorize_patient(task.observation.parsing_version.document.patient, request.user, "manage")
        request.patient, request.patient_access = access.patient, access
        manages_patient = True
    except PermissionDenied:
        manages_patient = False
    if request.method == "POST":
        if request.POST.get("action") == "ASSIGN":
            reviewer = get_object_or_404(get_user_model(), pk=request.POST.get("reviewer"))
            assign_review_task(request.user, task.pk, reviewer=reviewer, expected_revision=_expected(request))
        else:
            transition_review_task(request.user, task.pk, action=request.POST.get("action"),
                                   changes=_changes(request), expected_revision=_expected(request),
                                   resolved_issues=request.POST.getlist("resolved_issues"))
        return redirect("labs:review_task", task.pk)
    # Compute quality for the authorized target using the same server-side context
    # as owner reads. Peer/history rows never enter the template context or links.
    context = _observation_context(task.observation, include_patient_context=True)
    context["issues"] = tuple({**item, "details": "已审核规则提示需核对当前结果，相关背景资料不在本任务展示。"}
        if item["code"] in {"internal_conflict", "magnitude_suspect"} else item for item in context["issues"])
    context.update(task=task, owner=manages_patient,
                   events=task.events.select_related("author"), reviewable_issues=REVIEWABLE_ISSUES)
    response = _render(request, "labs/review.html", context)
    return _review_read_response(request, response, [task])


def _source(row, field, automatic=False):
    if field not in VALUE_FIELDS:
        raise Http404
    effective = effective_observation(row)
    identity = effective.value_sources[field]
    source_row = row if automatic else get_object_or_404(
        observation_queryset(), pk=identity["observation_id"], parsing_version__document_id=row.parsing_version.document_id,
    )
    field_evidence = source_row.field_evidence.get(field, {})
    page_number = field_evidence.get("page_number", source_row.document_page.page_number)
    if not isinstance(page_number, int) or not 1 <= page_number <= row.parsing_version.document.page_count:
        raise Http404
    # Missing field-level evidence degrades to a page; row OCR boxes are not exact field locations.
    polygon = field_evidence.get("polygon") if field_evidence.get("precision") == "region" else None
    return source_row, page_number, _highlight_rect(polygon)


def _source_response(request, row, field, *, task=None, image=False):
    source_row, page, rect = _source(row, field, request.GET.get("automatic") == "1")
    document = row.parsing_version.document
    if image:
        try:
            with closing(get_object_store().open_private(document.original_object_key)) as source:
                payload = render_page(source, document.content_type, page)
        except (UploadDomainError, ImproperlyConfigured, OSError, PreviewUnavailable):
            return _protect_page_image(HttpResponse("原件暂时无法打开，请重试。", status=503))
        # Revocation/deletion/version changes during rendering must stop this response too.
        if task:
            actor = get_user_model().objects.filter(pk=request.user.pk, is_active=True).first()
            if actor is None:
                raise PermissionDenied
            # A newly loaded actor has neither stale is_staff nor permission caches.
            get_review_task(actor, task.pk)
        else:
            _owner_row(request, row.pk)
        return _protect_page_image(HttpResponse(payload, content_type="image/png"))
    image_url = reverse("labs:review_source_image" if task else "labs:observation_source_image", args=(task.pk if task else row.pk, field))
    if request.GET.get("automatic") == "1":
        image_url += "?automatic=1"
    response = _render(request, "labs/source.html", {
        "source_row": source_row, "image_url": image_url, "page": page, "highlight_rect": rect,
        "location_label": "字段区域定位" if rect else "页面定位（无法精确定位字段）",
        "polygon": source_row.field_evidence.get(field, {}).get("polygon") if rect else None,
        "polygon_points": " ".join(f"{point[0]},{point[1]}" for point in source_row.field_evidence[field]["polygon"]) if rect else "",
        "source_base_template": "labs/source_embed_base.html" if request.GET.get("embed") == "1" else "labs/base.html",
    }, embeddable=request.GET.get("embed") == "1")
    return _review_read_response(request, response, [task]) if task else response


@patient_required
@require_GET
@workflow_errors
def observation_source(request, observation_id, field, image=False):
    return _source_response(request, _owner_row(request, observation_id), field, image=image)


@login_required
@require_GET
@workflow_errors
def review_source(request, task_id, field, image=False):
    task = get_review_task(request.user, task_id)
    return _source_response(request, task.observation, field, task=task, image=image)


@patient_required
@require_POST
@workflow_errors
def activate_version(request, version_id):
    target = get_object_or_404(ParsingVersion, pk=version_id, document__patient=request.patient, document__deleted_at__isnull=True)
    with transaction.atomic():
        authorize_patient(request.patient, request.user, "write", lock=True)
        document, _ = lock_document_aggregate(target.document_id)
        if document is None or document.deleted_at is not None or not document.patient.account.is_active:
            raise PermissionDenied
        active = document.parsing_versions.filter(active=True).first()
        if not active or str(active.pk) != request.POST.get("expected_active"):
            raise RevisionConflict("解析版本已经更新，请刷新后重试。")
        ParsingVersion.objects.activate(target)
    return redirect("documents:document_summary", document.pk)


def _manager(request, *, fresh=False):
    if not request.user.is_active or not request.user.is_staff or not request.user.groups.filter(name=Role.DICTIONARY_MANAGER.value).exists():
        raise PermissionDenied
    verified = None
    state = request.session.get("labs_second_factor", {})
    if isinstance(state, dict) and state.get("account") == str(request.user.pk):
        try:
            verified = datetime.fromisoformat(state.get("verified_at", ""))
        except (ValueError, TypeError):
            pass
    if fresh:
        authorize(request.user, Action.PUBLISH_DICTIONARY, totp_verified_at=verified)
    return verified


@login_required
@require_http_methods(["GET", "POST"])
@workflow_errors
def second_factor(request):
    from apps.accounts.authentication import InvalidCredentials, begin_password_login, complete_password_login
    from apps.accounts.providers import get_sms_provider
    from apps.accounts.services import OtpError, ThrottledPassword
    from apps.core.client_ip import get_client_ip

    _manager(request)
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "SEND":
                request.session.pop("labs_second_factor", None)
                request.session.pop("labs_second_factor_pending", None)
                pending = begin_password_login(request.POST.get("phone", ""), request.POST.get("password", ""), get_client_ip(request), get_sms_provider())
                if pending.account_id != request.user.pk:
                    raise InvalidCredentials
                request.session["labs_second_factor_pending"] = {"account": str(pending.account_id), "challenge": pending.challenge_id}
            else:
                pending = request.session.get("labs_second_factor_pending", {})
                if pending.get("account") != str(request.user.pk):
                    raise InvalidCredentials
                complete_password_login(pending["challenge"], request.POST.get("code", ""), request.user.pk)
                request.session.pop("labs_second_factor_pending", None)
                request.session["labs_second_factor"] = {"account": str(request.user.pk), "verified_at": timezone.now().isoformat()}
                return redirect("labs:dictionary")
        except (InvalidCredentials, OtpError, ThrottledPassword, KeyError):
            error = "身份验证未通过，请检查当前账户的手机号、密码或验证码。"
    return _render(request, "labs/second_factor.html", {"error": error, "pending": bool(request.session.get("labs_second_factor_pending"))}, status=400 if error else 200)


@login_required
@require_GET
@workflow_errors
def dictionary(request):
    _manager(request)
    candidates = []
    for candidate in DictionaryCandidate.objects.select_related("patient"):
        if workflow.candidate_sources(request.user, candidate).exists():
            candidates.append(candidate)
    return _render(request, "labs/dictionary.html", {"candidates": candidates, "releases": DictionaryRelease.objects.prefetch_related('evaluation_events').order_by("-published_at"), "active_hash": current_dictionary().content_hash})


@login_required
@require_http_methods(["GET", "POST"])
@workflow_errors
def dictionary_candidate(request, candidate_id):
    _manager(request)
    candidate = workflow.get_dictionary_candidate(request.user, candidate_id)
    if request.method == "POST":
        workflow.review_candidate(request.user, candidate.pk, decision=request.POST.get("decision"),
                                  definition=json.loads(request.POST.get("definition") or "{}"),
                                  rules=json.loads(request.POST.get("rules") or "[]"), rationale=request.POST.get("rationale", ""),
                                  expected_revision=_expected(request), totp_verified_at=_manager(request, fresh=True))
        return redirect("labs:dictionary_candidate", candidate.pk)
    sources = []
    for source in workflow.candidate_sources(request.user, candidate):
        if accessible_patients(request.user).filter(pk=candidate.patient_id).exists():
            url = reverse("labs:observation_source", args=(source.observation_id, "raw_name"))
        else:
            url = ""
            for task in source.observation.review_tasks.filter(reviewer=request.user):
                try:
                    get_review_task(request.user, task.pk)
                    url = reverse("labs:review_source", args=(task.pk, "raw_name"))
                    break
                except (PermissionDenied, RevisionConflict):
                    continue
        if url:
            sources.append({"url": url, "page": source.observation.document_page.page_number})
    return _render(request, "labs/candidate.html", {"candidate": candidate, "sources": sources,
        "definition_json": json.dumps(candidate.definition, ensure_ascii=False, indent=2),
        "rules_json": json.dumps(candidate.rules, ensure_ascii=False, indent=2), "events": candidate.events.order_by("sequence")})


@login_required
@require_POST
@workflow_errors
def dictionary_preview(request):
    _manager(request, fresh=True)
    identities = request.POST.getlist("candidate_ids")
    version = request.POST.get("version", "")
    preview = workflow.preview_dictionary(request.user, candidate_ids=identities, version=version)
    return _render(request, "labs/dictionary_preview.html", {"preview": preview, "preview_json": json.dumps({key: value for key, value in preview.items() if key != "payload"}, ensure_ascii=False, indent=2),
        "candidate_ids": identities, "version": version})


@login_required
@require_POST
@workflow_errors
def dictionary_publish(request):
    verified = _manager(request, fresh=True)
    if not request.POST.get("expected_preview_hash"):
        raise ValidationError("请先查看差异和回归结果。")
    workflow.publish_dictionary(request.user, version=request.POST.get("version"), candidate_ids=request.POST.getlist("candidate_ids"),
                                expected_active_hash=request.POST.get("expected_active_hash"), expected_preview_hash=request.POST["expected_preview_hash"], totp_verified_at=verified)
    return redirect("labs:dictionary")


@login_required
@require_POST
@workflow_errors
def dictionary_rollback(request, release_id):
    get_object_or_404(DictionaryRelease, pk=release_id)
    workflow.rollback_dictionary(request.user, release_id, expected_active_hash=request.POST.get("expected_active_hash"), totp_verified_at=_manager(request, fresh=True))
    return redirect("labs:dictionary")
