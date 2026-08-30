from urllib.parse import unquote, urlsplit

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
    if not isinstance(value, str) or not value:
        return ""
    decoded = unquote(value)
    if "\\" in decoded or any(ord(char) < 32 for char in decoded):
        return ""
    parsed = urlsplit(value)
    if not value.startswith("/") or value.startswith("//") or parsed.scheme or parsed.netloc:
        return ""
    if not url_has_allowed_host_and_scheme(value, {request.get_host()}, request.is_secure()):
        return ""
    return value


def _render_login(request, *, destination="", error="", request_accepted=False, status=""):
    return render(
        request,
        "accounts/login.html",
        {"form": LoginForm(initial={"next": destination}), "error": error, "status": status, "request_accepted": request_accepted},
    )


def login_page(request):
    destination = _safe_next(request, request.GET.get("next", ""))
    if request.user.is_authenticated:
        return redirect(destination or "/")
    return _render_login(request, destination=destination)


@require_POST
def request_code(request):
    form = PhoneRequestForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", ""))
    if not form.is_valid():
        return _render_login(request, destination=destination, error="请输入有效的中国大陆手机号。")
    try:
        request_otp(form.cleaned_data["phone"], request.META.get("REMOTE_ADDR", ""), get_sms_provider())
    except InvalidPhone:
        return _render_login(request, destination=destination, error="请输入有效的中国大陆手机号。")
    except ThrottledOtp:
        return _render_login(request, destination=destination, error="操作过于频繁，请稍后再试。")
    except DeliveryFailed:
        return _render_login(request, destination=destination, error="网络繁忙，请稍后再试。")
    return _render_login(request, destination=destination, request_accepted=True, status="验证码已发送，请在五分钟内完成登录。")


@require_POST
def verify_code(request):
    form = VerifyForm(request.POST)
    destination = _safe_next(request, request.POST.get("next", ""))
    if not form.is_valid():
        return _render_login(request, destination=destination, error="验证码无效或已过期。")
    try:
        account = verify_otp(form.cleaned_data["phone"], form.cleaned_data["code"])
    except (InvalidOtp, LockedOtp, InvalidPhone, ValueError):
        return _render_login(request, destination=destination, error="验证码无效或已过期。")
    login(request, account)
    initialize_session(request)
    return redirect(destination or "/")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/login/")


def privacy_page(request):
    return render(request, "accounts/privacy.html")
