from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.deletion import AccountDeletionUnavailable, request_account_deletion
from apps.accounts.tasks import safe_enqueue_account_deletion
from apps.accounts.views import _safe_next
from apps.analytics.events import record_product_event
from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html
from apps.documents.selectors import home_task_cards, recent_documents, task_status_cards
from apps.documents.tasks import safe_enqueue_document_deletion
from apps.operations.audit import record_audit_event
from apps.notifications.services import revoke_push_subscriptions

from .forms import (
    DisplayNameForm,
    NotificationPreferenceForm,
    OnboardingForm,
    ProductFeedbackForm,
    ReconsentForm,
)
from .models import Patient, PatientPreference
from .policies import policy_items, policy_unavailable_response
from .services import (
    ConsentPolicyConflict,
    MissingRequiredConsent,
    account_needs_onboarding,
    create_patient_space,
    missing_current_consents,
)
from .profile import patient_preferences, quota_summary, save_product_feedback, update_display_name


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
                if not is_reconsent:
                    record_product_event(
                        "patient_created",
                        {"duration_bucket": "unknown"},
                        account_id=request.user.pk,
                    )
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
@require_GET
def home(request):
    task_cards = home_task_cards(request.patient)
    preferences = patient_preferences(request.patient)
    return protect_sensitive_html(
        render(
            request,
            "patients/home.html",
            {
                "current_section": "home",
                "recent_documents": recent_documents(request.patient),
                "task_cards": task_cards,
                "browser_notifications_enabled": preferences.browser_notifications_enabled,
            },
        )
    )


def _profile_response(request, *, name_form=None, feedback_form=None, status=200):
    preferences = patient_preferences(request.patient)
    return protect_sensitive_html(
        render(
            request,
            "patients/profile.html",
            {
                "current_section": "profile",
                "name_form": name_form or DisplayNameForm(initial={"display_name": request.patient.display_name}),
                "feedback_form": feedback_form or ProductFeedbackForm(),
                "preferences": preferences,
                "quota": quota_summary(request.patient),
                "name_saved": request.GET.get("name") == "saved",
                "feedback_saved": request.GET.get("feedback") == "thanks",
                "notification_status": request.GET.get("notification", ""),
            },
            status=status,
        )
    )


@patient_required
@require_GET
def profile(request):
    return _profile_response(request)


@patient_required
@require_POST
def update_profile_name(request):
    form = DisplayNameForm(request.POST)
    if not form.is_valid():
        return _profile_response(request, name_form=form, status=400)
    patient = update_display_name(request.patient.pk, form.cleaned_data["display_name"])
    record_audit_event(request.user.pk, "patient_name_changed", patient.pk, "succeeded")
    request.patient = patient
    return redirect("/me/?name=saved#patient-name")


@patient_required
@require_POST
def submit_product_feedback(request):
    form = ProductFeedbackForm(request.POST)
    if not form.is_valid():
        return _profile_response(request, feedback_form=form, status=400)
    feedback = save_product_feedback(request.patient, form.cleaned_data["message"])
    record_product_event("product_feedback", {"category": "general"}, account_id=request.user.pk)
    record_audit_event(request.user.pk, "product_feedback_created", feedback.pk, "succeeded")
    return redirect("/me/?feedback=thanks#product-feedback")


@patient_required
@require_POST
def update_notification_preference(request):
    form = NotificationPreferenceForm(request.POST)
    if not form.is_valid():
        return redirect("/me/?notification=unavailable#notifications")
    permission = request.POST.get("permission", "")
    enabled = form.cleaned_data["enabled"] and permission == "granted"
    with transaction.atomic():
        patient = Patient.objects.select_for_update().get(pk=request.patient.pk)
        preference, _created = PatientPreference.objects.get_or_create(patient=patient)
        preference.browser_notifications_enabled = enabled
        if form.cleaned_data["prompted"] or permission:
            preference.browser_notification_prompted_at = timezone.now()
        preference.save(
            update_fields=["browser_notifications_enabled", "browser_notification_prompted_at", "updated_at"]
        )
        if not enabled:
            revoke_push_subscriptions(patient)
        record_product_event(
            "browser_notification_enabled",
            {
                "browser_family": request.POST.get("browser_family", "unknown")
                if request.POST.get("browser_family", "unknown") in {"chrome", "edge", "safari", "other", "unknown"}
                else "unknown",
                "authorization_result": "granted" if enabled else ("denied" if permission == "denied" else "disabled"),
            },
            account_id=request.user.pk,
        )
        record_audit_event(
            request.user.pk,
            "notification_preference_changed",
            request.patient.pk,
            "succeeded",
        )
    if permission == "denied":
        status = "denied"
    else:
        status = "on" if enabled else "off"
    return redirect(f"/me/?notification={status}#notifications")


@patient_required
@require_http_methods(["GET", "POST"])
def delete_account(request):
    if request.method == "GET":
        return protect_sensitive_html(
            render(request, "patients/delete_account_confirm.html", {"current_section": "profile"})
        )
    if request.POST.get("confirmation") != "delete-account":
        return protect_sensitive_html(
            render(
                request,
                "patients/delete_account_confirm.html",
                {"current_section": "profile", "confirmation_error": True},
                status=400,
            )
        )
    try:
        request_account_deletion(
            request.user.pk,
            document_dispatch=safe_enqueue_document_deletion,
            account_dispatch=safe_enqueue_account_deletion,
        )
    except AccountDeletionUnavailable:
        logout(request)
        response = redirect("/account-deleted/")
        response["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        return response
    logout(request)
    response = redirect("/account-deleted/")
    response["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    return response


@require_GET
def account_deleted(request):
    return render(request, "patients/account_deleted.html")


@patient_required
@require_GET
def tasks_placeholder(request):
    return protect_sensitive_html(
        render(
            request,
            "patients/tasks.html",
            {
                "current_section": "tasks",
                "task_cards": task_status_cards(request.patient),
            },
        )
    )


@require_GET
def sensitive_information(request):
    try:
        policy = policy_items(["sensitive_data"])[0]
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    return render(request, "patients/sensitive_information.html", {"policy": policy})
