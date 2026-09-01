from django.contrib.auth import login, logout
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.analytics.events import record_product_event

from .authentication import (
    EnrollmentUnavailable,
    ExistingAccountRequiresLogin,
    InvalidCredentials,
    begin_first_use,
    begin_password_login,
    complete_first_use_verification,
    complete_password_login,
    create_or_upgrade_account,
)
from .flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    VERIFIED_PHONE_SESSION_KEY,
    clear_enrollment_state,
    load_pending_enrollment,
    load_pending_mfa,
    load_verified_enrollment,
    safe_destination,
    store_pending_enrollment,
    store_pending_mfa,
    store_verified_phone,
)
from .forms import FirstUsePhoneForm, MfaForm, PasswordLoginForm, SetPasswordForm
from .models import OtpChallenge
from .phone import InvalidPhone
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


def _clear_authentication_flow_state(request):
    for key in (
        SIGN_IN_PENDING_MFA_SESSION_KEY,
        ENROLLMENT_PENDING_MFA_SESSION_KEY,
        PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
        VERIFIED_PHONE_SESSION_KEY,
    ):
        request.session.pop(key, None)


def _complete_authenticated_session(request, account, destination):
    from apps.patients.policies import ConsentPolicyConflict, policy_unavailable_response
    from apps.patients.services import account_needs_onboarding

    try:
        needs_onboarding = account_needs_onboarding(account)
    except ConsentPolicyConflict:
        return policy_unavailable_response(request)
    login(request, account, backend="django.contrib.auth.backends.ModelBackend")
    initialize_session(request)
    _clear_authentication_flow_state(request)
    request.session.pop("post_onboarding_next", None)
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
        if destination != "/":
            request.session["post_onboarding_next"] = destination
        return redirect("/onboarding/")
    return redirect(destination)


def _render_first_use_phone(request, *, destination="", error="", response_status=200):
    return render(
        request,
        "accounts/first_use_phone.html",
        {"form": FirstUsePhoneForm(initial={"next": destination}), "error": error},
        status=response_status,
    )


def _render_first_use_verify(request, *, error="", response_status=200):
    return render(
        request,
        "accounts/first_use_verify.html",
        {"form": MfaForm(), "error": error},
        status=response_status,
    )


def _render_set_password(request, *, form=None, error="", guidance="", response_status=200):
    return render(
        request,
        "accounts/set_password.html",
        {"form": form or SetPasswordForm(), "error": error, "guidance": guidance},
        status=response_status,
    )


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
    return _complete_authenticated_session(request, account, pending.destination)


@require_http_methods(["GET", "POST"])
def first_use_phone(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    if request.method == "GET":
        destination = _safe_next(request, request.GET.get("next", ""))
        return _render_first_use_phone(request, destination=destination)

    clear_enrollment_state(request)
    form = FirstUsePhoneForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", "")) or "/"
    if not form.is_valid():
        return _render_first_use_phone(
            request,
            destination=destination,
            error="手机号无效，请检查后重试。",
            response_status=400,
        )
    try:
        challenge = begin_first_use(
            form.cleaned_data["phone"],
            request.META.get("REMOTE_ADDR", ""),
            get_sms_provider(),
        )
    except (InvalidPhone, ValueError, ThrottledOtp):
        return _render_first_use_phone(
            request,
            destination=destination,
            error="暂时无法发送验证码，请稍后重试。",
            response_status=400,
        )
    except DeliveryFailed:
        return _render_first_use_phone(
            request,
            destination=destination,
            error="暂时无法发送验证码，请检查网络后重试。",
            response_status=400,
        )
    store_pending_enrollment(request, challenge.pk, destination)
    return redirect("/login/first-use/verify/")


@require_http_methods(["GET", "POST"])
def verify_first_use(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    pending = load_pending_enrollment(request)
    if pending is None:
        clear_enrollment_state(request)
        return _render_first_use_verify(
            request,
            error="验证码无效，请重新开始。",
            response_status=400,
        )
    if request.method == "GET":
        return _render_first_use_verify(request)

    form = MfaForm(request.POST)
    if not form.is_valid():
        clear_enrollment_state(request)
        return _render_first_use_verify(
            request,
            error="验证码无效，请重新开始。",
            response_status=400,
        )
    try:
        challenge = complete_first_use_verification(pending.challenge_id, form.cleaned_data["code"])
    except InvalidOtp:
        return _render_first_use_verify(
            request,
            error="验证码无效，请重新输入。",
            response_status=400,
        )
    except (LockedOtp, ValueError):
        clear_enrollment_state(request)
        return _render_first_use_verify(
            request,
            error="验证码无效，请重新开始。",
            response_status=400,
        )
    store_verified_phone(request, challenge.pk)
    return redirect("/login/first-use/password/")


@require_http_methods(["GET", "POST"])
def first_use_password(request):
    unavailable = _policy_unavailable_if_invalid(request)
    if unavailable is not None:
        return unavailable
    pending = load_verified_enrollment(request)
    if pending is None:
        return _render_set_password(
            request,
            error="验证已失效，请重新开始。",
            response_status=400,
        )
    if request.method == "GET":
        return _render_set_password(request)

    form = SetPasswordForm(request.POST)
    if not form.is_valid():
        return _render_set_password(request, form=form, response_status=400)
    challenge = OtpChallenge.objects.filter(pk=pending.challenge_id).first()
    if challenge is None:
        clear_enrollment_state(request)
        return _render_set_password(
            request,
            error="验证已失效，请重新开始。",
            response_status=400,
        )
    try:
        account = create_or_upgrade_account(challenge, form.cleaned_data["password"])
    except ValidationError as exc:
        form.add_error("password", exc)
        return _render_set_password(request, form=form, response_status=400)
    except ExistingAccountRequiresLogin:
        clear_enrollment_state(request)
        return _render_set_password(
            request,
            guidance="This account already has a password. Use normal login or password reset.",
            response_status=400,
        )
    except EnrollmentUnavailable:
        clear_enrollment_state(request)
        return _render_set_password(
            request,
            error="暂时无法完成设置，请使用正常登录或密码重置。",
            response_status=400,
        )
    return _complete_authenticated_session(request, account, pending.destination)


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
