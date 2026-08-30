from urllib.parse import unquote, urlsplit, urlunsplit

from django.contrib.auth import login, logout
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import LoginForm, PhoneRequestForm, VerifyForm
from .phone import InvalidPhone
from .providers import get_sms_provider
from .services import DeliveryFailed, InvalidOtp, LockedOtp, ThrottledOtp, request_otp, verify_otp
from .session import initialize_session


def _safe_next(request, value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    parsed = urlsplit(value)
    path = parsed.path
    for _ in range(3):
        decoded_path = unquote(path)
        if decoded_path == path:
            break
        path = decoded_path
    else:
        return ""
    if "%" in path or "\\" in path or any(ord(char) < 32 for char in path):
        return ""
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
        return ""
    if any(segment in {".", ".."} for segment in path.split("/")):
        return ""
    canonical = urlunsplit(("", "", path, parsed.query, ""))
    if path in {"/login", "/logout"} or path.startswith(("/login/", "/logout/")):
        return ""
    if not url_has_allowed_host_and_scheme(canonical, {request.get_host()}, request.is_secure()):
        return ""
    return canonical


def _render_login(request, *, destination="", phone="", error="", request_accepted=False, status=""):
    return render(
        request,
        "accounts/login.html",
        {"form": LoginForm(initial={"next": destination, "phone": phone}), "error": error, "status": status, "request_accepted": request_accepted},
    )


def login_page(request):
    destination = _safe_next(request, request.GET.get("next", ""))
    if request.user.is_authenticated:
        from apps.patients.services import account_needs_onboarding

        if account_needs_onboarding(request.user):
            if destination:
                request.session["post_onboarding_next"] = destination
            return redirect("/onboarding/")
        return redirect(destination or "/")
    return _render_login(request, destination=destination)


@require_POST
def request_code(request):
    form = PhoneRequestForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", ""))
    if not form.is_valid():
        return _render_login(request, destination=destination, error="请输入正确的手机号")
    phone = form.cleaned_data["phone"]
    try:
        request_otp(phone, request.META.get("REMOTE_ADDR", ""), get_sms_provider())
    except InvalidPhone:
        return _render_login(request, destination=destination, error="请输入正确的手机号")
    except ThrottledOtp:
        return _render_login(request, destination=destination, phone=phone, error="操作过于频繁，请稍后再试")
    except DeliveryFailed:
        return _render_login(request, destination=destination, phone=phone, error="暂时无法登录，请检查网络后重试")
    return _render_login(request, destination=destination, phone=phone, request_accepted=True, status="验证码已发送，请在五分钟内完成登录。")


@require_POST
def verify_code(request):
    form = VerifyForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", ""))
    if not form.is_valid():
        return _render_login(request, destination=destination, error="验证码无效，请重新获取")
    phone = form.cleaned_data["phone"]
    try:
        account = verify_otp(form.cleaned_data["phone"], form.cleaned_data["code"])
    except (InvalidOtp, LockedOtp, InvalidPhone, ValueError):
        return _render_login(request, destination=destination, phone=phone, error="验证码无效，请重新获取")
    login(request, account, backend="django.contrib.auth.backends.ModelBackend")
    initialize_session(request)
    from apps.patients.services import account_needs_onboarding

    if account_needs_onboarding(account):
        if destination:
            request.session["post_onboarding_next"] = destination
        return redirect("/onboarding/")
    return redirect(destination or "/")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/login/")


def privacy_page(request):
    return render(request, "accounts/privacy.html")
