from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import _safe_next
from apps.core.decorators import patient_required
from apps.documents.selectors import home_task_cards, recent_documents

from .forms import OnboardingForm, ReconsentForm
from .models import Patient
from .policies import policy_items, policy_unavailable_response
from .services import (
    ConsentPolicyConflict,
    MissingRequiredConsent,
    account_needs_onboarding,
    create_patient_space,
    missing_current_consents,
)


def _request_evidence(request):
    return {"ip": request.META.get("REMOTE_ADDR", ""), "user_agent": request.META.get("HTTP_USER_AGENT", "")}


@login_required
@require_http_methods(["GET", "POST"])
def onboarding(request):
    try:
        try:
            request.user.patient
        except Patient.DoesNotExist:
            is_reconsent = False
            missing_types = None
        else:
            is_reconsent = True
            missing_types = missing_current_consents(request.user)
            if not missing_types:
                return redirect("/")
        rendered_policies = policy_items(missing_types if is_reconsent else None)
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    form_class = ReconsentForm if is_reconsent else OnboardingForm
    if request.method == "POST":
        form = form_class(missing_types, request.POST) if is_reconsent else form_class(request.POST)
        if form.is_valid():
            try:
                create_patient_space(
                    request.user,
                    form.cleaned_data.get("display_name"),
                    form.cleaned_data,
                    _request_evidence(request),
                )
            except ConsentPolicyConflict:
                return policy_unavailable_response(request)
            except (MissingRequiredConsent, ValueError):
                form.add_error(None, "请完整确认后继续")
            else:
                destination = _safe_next(request, request.session.pop("post_onboarding_next", ""))
                return redirect(destination or "/")
    else:
        form = form_class(missing_types) if is_reconsent else form_class()
    return render(
        request,
        "patients/onboarding.html",
        {
            "form": form,
            "is_reconsent": is_reconsent,
            "policy_items": rendered_policies,
        },
    )


@patient_required
def home(request):
    task_cards = home_task_cards(request.patient)
    return render(
        request,
        "patients/home.html",
        {
            "current_section": "home",
            "recent_documents": recent_documents(request.patient),
            "task_cards": task_cards,
        },
    )


@patient_required
def records_placeholder(request):
    return render(
        request,
        "patients/app_placeholder.html",
        {"page_title": "病案", "placeholder_copy": "病案功能暂未开放，当前不会展示任何资料。", "show_upload_control": False, "current_section": "records"},
    )


@patient_required
def profile_placeholder(request):
    return render(
        request,
        "patients/app_placeholder.html",
        {"page_title": "我的", "placeholder_copy": "个人设置功能暂未开放。", "show_upload_control": False, "current_section": "profile"},
    )


@patient_required
def tasks_placeholder(request):
    return redirect("/#home-tasks-title")


def sensitive_information(request):
    try:
        policy = policy_items(["sensitive_data"])[0]
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    return render(request, "patients/sensitive_information.html", {"policy": policy})
