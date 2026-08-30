from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import _safe_next

from .forms import OnboardingForm, ReconsentForm
from .models import Patient
from .policies import policy_items
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
        request.user.patient
    except Patient.DoesNotExist:
        is_reconsent = False
        missing_types = None
    else:
        is_reconsent = True
        missing_types = missing_current_consents(request.user)
        if not missing_types:
            return redirect("/")
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
            except (MissingRequiredConsent, ConsentPolicyConflict, ValueError):
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
            "policy_items": policy_items(missing_types if is_reconsent else None),
        },
    )


@login_required
def home(request):
    if account_needs_onboarding(request.user):
        return redirect("/onboarding/")
    return render(request, "patients/home_placeholder.html")


def sensitive_information(request):
    return render(request, "patients/sensitive_information.html", {"policy": policy_items(["sensitive_data"])[0]})
