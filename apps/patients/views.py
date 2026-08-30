from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import _safe_next

from .forms import OnboardingForm
from .services import MissingRequiredConsent, account_needs_onboarding, create_patient_space


def _request_evidence(request):
    return {"ip": request.META.get("REMOTE_ADDR", ""), "user_agent": request.META.get("HTTP_USER_AGENT", "")}


@login_required
@require_http_methods(["GET", "POST"])
def onboarding(request):
    if request.method == "POST":
        form = OnboardingForm(request.POST)
        if form.is_valid():
            try:
                create_patient_space(request.user, form.cleaned_data["display_name"], form.cleaned_data, _request_evidence(request))
            except (MissingRequiredConsent, ValueError):
                form.add_error(None, "请完整确认后继续")
            else:
                destination = _safe_next(request, request.session.pop("post_onboarding_next", ""))
                return redirect(destination or "/")
    else:
        if not account_needs_onboarding(request.user):
            return redirect("/")
        form = OnboardingForm()
    return render(request, "patients/onboarding.html", {"form": form})


@login_required
def home(request):
    if account_needs_onboarding(request.user):
        return redirect("/onboarding/")
    return render(request, "patients/home_placeholder.html")


def sensitive_information(request):
    return render(request, "patients/sensitive_information.html")
