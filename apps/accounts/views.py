import secrets
from urllib.parse import urlencode
from uuid import uuid4

from django.contrib.auth import login, logout
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.analytics.events import record_product_event
from apps.core.client_ip import get_client_ip

from .authentication import (
    EnrollmentUnavailable,
    ExistingAccountRequiresLogin,
    InvalidCredentials,
    PasswordResetUnavailable,
    begin_first_use,
    begin_password_login,
    begin_password_reset,
    complete_first_use_verification,
    complete_password_login,
    complete_password_reset_verification,
    complete_verified_password_reset,
    create_or_upgrade_account,
)
from .flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_DECOY_ATTEMPTS_SESSION_KEY,
    PASSWORD_RESET_FAILURE_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    VERIFIED_PASSWORD_RESET_SESSION_KEY,
    VERIFIED_PHONE_SESSION_KEY,
    clear_enrollment_state,
    clear_password_reset_state,
    load_pending_enrollment,
    load_pending_mfa,
    load_pending_password_reset,
    load_verified_enrollment,
    load_verified_password_reset,
    safe_destination,
    store_pending_enrollment,
    store_pending_mfa,
    store_pending_password_reset,
    store_verified_password_reset,
    store_verified_phone,
)
from .forms import (
    FirstUsePhoneForm,
    MfaForm,
    PasswordLoginForm,
    PasswordResetRequestForm,
    ResetPasswordForm,
    SetPasswordForm,
)
from .models import Account, OtpChallenge
from .phone import InvalidPhone
from .providers import get_sms_provider
from .otp import burn_dummy_otp_work
from .services import DeliveryFailed, InvalidOtp, LockedOtp, ThrottledOtp
from .session import initialize_session
from .sms_delivery import allow_password_reset_request, queue_decoy_password_reset


def _safe_next(request, value):
    return safe_destination(request, value)


RESET_REQUEST_STATUS = "如果该手机号可用，我们已发送验证码，请按页面提示继续。"
RESET_VERIFY_ERROR = "验证码无效，请重试。"


def _render_login(request, *, destination="", error="", status="", response_status=200):
    return render(
        request,
        "accounts/login.html",
        {
            "form": PasswordLoginForm(initial={"next": destination}),
            "destination": destination,
            "error": error,
            "status": status,
        },
        status=response_status,
    )


def _render_mfa(request, *, error="", response_status=200):
    return render(request, "accounts/login_mfa.html", {"form": MfaForm(), "error": error}, status=response_status)


def _clear_authentication_flow_state(request):
    for key in (
        SIGN_IN_PENDING_MFA_SESSION_KEY,
        ENROLLMENT_PENDING_MFA_SESSION_KEY,
        PASSWORD_RESET_DECOY_ATTEMPTS_SESSION_KEY,
        PASSWORD_RESET_FAILURE_SESSION_KEY,
        PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
        VERIFIED_PHONE_SESSION_KEY,
        VERIFIED_PASSWORD_RESET_SESSION_KEY,
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


def _render_password_reset_request(request, *, destination="/", status=""):
    return render(
        request,
        "accounts/forgot_password.html",
        {
            "form": PasswordResetRequestForm(initial={"next": destination}),
            "status": status,
        },
    )


def _render_password_reset_verify(request, *, error="", response_status=200):
    if error:
        # Every non-advancing verification POST refreshes the same opaque,
        # non-authorizing session marker so response cookies cannot reveal
        # whether reset state or a database challenge existed.
        request.session[PASSWORD_RESET_FAILURE_SESSION_KEY] = True
    return render(
        request,
        "accounts/reset_verify.html",
        {"form": MfaForm(), "error": error, "status": RESET_REQUEST_STATUS},
        status=response_status,
    )


def _render_reset_password(request, account, *, form=None, error="", response_status=200):
    return render(
        request,
        "accounts/reset_password.html",
        {"form": form or ResetPasswordForm(account=account), "error": error},
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
    status = "密码已重置，请使用新密码登录。" if request.GET.get("password-reset") == "complete" else ""
    return _render_login(request, destination=destination, status=status)


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
            get_client_ip(request),
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
    except InvalidOtp:
        return _render_mfa(request, error="验证码无效，请重新登录", response_status=400)
    except (LockedOtp, ValueError):
        request.session.pop(SIGN_IN_PENDING_MFA_SESSION_KEY, None)
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
            get_client_ip(request),
            get_sms_provider(),
        )
    except (InvalidPhone, ValueError, LockedOtp, ThrottledOtp):
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
            guidance="该账号已设置密码，请使用正常登录或重设密码。",
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


@require_http_methods(["GET", "POST"])
def forgot_password(request):
    if request.method == "GET":
        destination = _safe_next(request, request.GET.get("next", "")) or "/"
        return _render_password_reset_request(request, destination=destination)

    clear_password_reset_state(request)
    form = PasswordResetRequestForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", "")) or "/"
    client_ip = get_client_ip(request)
    request_allowed = allow_password_reset_request(client_ip)
    if request_allowed and form.is_valid():
        try:
            pending = begin_password_reset(
                form.cleaned_data["phone"],
                client_ip,
            )
        except (InvalidPhone, ValueError, LockedOtp, ThrottledOtp, DeliveryFailed):
            pending = None
    else:
        pending = None
    if pending is None:
        if request_allowed:
            queue_decoy_password_reset()
        else:
            burn_dummy_otp_work()
        store_pending_password_reset(
            request,
            uuid4(),
            -(secrets.randbelow(2**63 - 1) + 1),
            destination,
        )
        request.session[PASSWORD_RESET_DECOY_ATTEMPTS_SESSION_KEY] = 0
    else:
        store_pending_password_reset(
            request,
            pending.account_id,
            pending.challenge_id,
            destination,
        )
    return redirect("/login/forgot-password/verify/")


@require_http_methods(["GET", "POST"])
def verify_password_reset(request):
    pending = load_pending_password_reset(request)
    if request.method == "GET":
        return _render_password_reset_verify(request)
    if pending is None:
        burn_dummy_otp_work()
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )

    form = MfaForm(request.POST)
    if not form.is_valid():
        burn_dummy_otp_work()
        clear_password_reset_state(request)
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )
    if pending.challenge_id < 0:
        burn_dummy_otp_work()
        attempts = request.session.get(
            PASSWORD_RESET_DECOY_ATTEMPTS_SESSION_KEY,
            0,
        )
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            attempts = 0
        attempts += 1
        if attempts >= 5:
            clear_password_reset_state(request)
        else:
            request.session[PASSWORD_RESET_DECOY_ATTEMPTS_SESSION_KEY] = attempts
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )
    try:
        challenge = complete_password_reset_verification(
            pending.challenge_id,
            form.cleaned_data["code"],
            pending.account_id,
        )
    except InvalidOtp:
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )
    except (LockedOtp, ValueError):
        clear_password_reset_state(request)
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )
    try:
        store_verified_password_reset(request, challenge.account_id, challenge.pk)
    except ValueError:
        clear_password_reset_state(request)
        return _render_password_reset_verify(
            request,
            error=RESET_VERIFY_ERROR,
            response_status=400,
        )
    return redirect("/login/forgot-password/new-password/")


@require_http_methods(["GET", "POST"])
def new_password(request):
    verified = load_verified_password_reset(request)
    account = None if verified is None else Account.objects.filter(
        pk=verified.account_id,
        is_active=True,
    ).first()
    if verified is None or account is None:
        clear_password_reset_state(request)
        return _render_reset_password(
            request,
            account,
            error="验证已失效，请重新开始。",
            response_status=400,
        )
    if request.method == "GET":
        return _render_reset_password(request, account)

    form = ResetPasswordForm(request.POST, account=account)
    if not form.is_valid():
        return _render_reset_password(request, account, form=form, response_status=400)
    try:
        complete_verified_password_reset(
            verified.account_id,
            verified.challenge_id,
            form.cleaned_data["password"],
        )
    except ValidationError as exc:
        form.add_error("password", exc)
        return _render_reset_password(request, account, form=form, response_status=400)
    except PasswordResetUnavailable:
        clear_password_reset_state(request)
        return _render_reset_password(
            request,
            account,
            error="验证已失效，请重新开始。",
            response_status=400,
        )
    destination = verified.destination
    _clear_authentication_flow_state(request)
    request.session.flush()
    query = {"password-reset": "complete"}
    if destination != "/":
        query["next"] = destination
    return redirect(f"/login/?{urlencode(query)}")


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
