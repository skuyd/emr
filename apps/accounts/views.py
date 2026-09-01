from django.contrib.auth import login, logout
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.analytics.events import record_product_event

from .authentication import InvalidCredentials, begin_password_login, complete_password_login
from .flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    load_pending_mfa,
    safe_destination,
    store_pending_mfa,
)
from .forms import MfaForm, PasswordLoginForm
from .providers import get_sms_provider
from .services import DeliveryFailed, InvalidOtp, LockedOtp, ThrottledOtp
from .session import initialize_session


def _safe_next(request, value):
    return safe_destination(request, value)


def _render_login(request, *, destination="", error="", response_status=200):
    return render(
        request,
        "accounts/login.html",
        {"form": PasswordLoginForm(initial={"next": destination}), "error": error},
        status=response_status,
    )


def _render_mfa(request, *, error="", response_status=200):
    return render(request, "accounts/login_mfa.html", {"form": MfaForm(), "error": error}, status=response_status)


def _policy_unavailable_if_invalid(request):
    from apps.patients.policies import ConsentPolicyConflict, consent_policies, policy_unavailable_response

    try:
        consent_policies()
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    return None


@require_GET
def login_page(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    destination = _safe_next(request, request.GET.get("next", ""))
    if request.user.is_authenticated:
        from apps.patients.policies import ConsentPolicyConflict, policy_unavailable_response
        from apps.patients.services import account_needs_onboarding

        try:
            needs_onboarding = account_needs_onboarding(request.user)
        except ConsentPolicyConflict:
            return policy_unavailable_response(request)
        if needs_onboarding:
            if destination:
                request.session["post_onboarding_next"] = destination
            return redirect("/onboarding/")
        return redirect(destination or "/")
    return _render_login(request, destination=destination)


@require_POST
def password_login(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    form = PasswordLoginForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", ""))
    if not form.is_valid():
        return _render_login(request, destination=destination, error="手机号或密码不正确", response_status=400)
    try:
        pending = begin_password_login(
            form.cleaned_data["phone"],
            form.cleaned_data["password"],
            request.META.get("REMOTE_ADDR", ""),
            get_sms_provider(),
        )
    except InvalidCredentials:
        return _render_login(request, destination=destination, error="手机号或密码不正确", response_status=400)
    except ThrottledOtp:
        return _render_login(request, destination=destination, error="暂时无法登录，请稍后再试", response_status=400)
    except DeliveryFailed:
        return _render_login(request, destination=destination, error="暂时无法登录，请检查网络后重试", response_status=400)
    store_pending_mfa(request, pending, destination)
    return redirect("/login/verify/")


@require_http_methods(["GET", "POST"])
def verify_login(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    pending = load_pending_mfa(request)
    if pending is None:
        return _render_mfa(request, error="验证码无效，请重新登录", response_status=400)
    if request.method == "GET":
        return _render_mfa(request)

    form = MfaForm(request.POST)
    if not form.is_valid():
        return _render_mfa(request, error="验证码无效，请重新登录", response_status=400)
    try:
        account = complete_password_login(pending.challenge_id, form.cleaned_data["code"], pending.account_id)
    except (InvalidOtp, LockedOtp, ValueError):
        return _render_mfa(request, error="验证码无效，请重新登录", response_status=400)
    from apps.patients.policies import ConsentPolicyConflict, policy_unavailable_response
    from apps.patients.services import account_needs_onboarding

    try:
        needs_onboarding = account_needs_onboarding(account)
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    login(request, account, backend="django.contrib.auth.backends.ModelBackend")
    initialize_session(request)
    for key in (
        SIGN_IN_PENDING_MFA_SESSION_KEY,
        ENROLLMENT_PENDING_MFA_SESSION_KEY,
        PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    ):
        request.session.pop(key, None)
    from apps.patients.models import Patient

    record_product_event(
        "login_succeeded",
        {
            "is_first_login": not Patient.objects.filter(account=account).exists(),
            "duration_bucket": "unknown",
        },
        account_id=account.pk,
    )
    if needs_onboarding:
        if pending.destination != "/":
            request.session["post_onboarding_next"] = pending.destination
        return redirect("/onboarding/")
    return redirect(pending.destination)


@require_POST
def logout_view(request):
    logout(request)
    response = redirect("/login/")
    response["Clear-Site-Data"] = '"cache", "storage"'
    return response


@require_GET
def privacy_page(request):
    from apps.patients.policies import ConsentPolicyConflict, policy_items, policy_unavailable_response

    try:
        policy = policy_items(["privacy"])[0]
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    return render(request, "accounts/privacy.html", {"policy": policy})
