from urllib.parse import urlsplit

from django.contrib.auth import login, logout
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import PhoneForm, VerifyForm
from .providers import get_sms_provider
from .services import DeliveryFailed, InvalidOtp, LockedOtp, ThrottledOtp, request_otp, verify_otp


def _safe_next(value):
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        return ""
    parsed = urlsplit(value)
    return value if value.startswith("/") and not value.startswith("//") and not parsed.scheme and not parsed.netloc else ""


def login_page(request):
    destination = _safe_next(request.GET.get("next", ""))
    if request.user.is_authenticated:
        return redirect(destination or "/")
    return render(request, "accounts/login.html", {"phone_form": PhoneForm(initial={"next": destination}), "verify_form": VerifyForm(initial={"next": destination})})


@require_POST
def request_code(request):
    form = PhoneForm(request.POST)
    destination = _safe_next(request.POST.get("next", ""))
    if form.is_valid():
        try:
            request_otp(form.cleaned_data["phone"], request.META.get("REMOTE_ADDR", ""), get_sms_provider())
        except (DeliveryFailed, ThrottledOtp, ValueError):
            pass
    return render(request, "accounts/login.html", {"phone_form": PhoneForm(initial={"next": destination}), "verify_form": VerifyForm(initial={"next": destination}), "message": "如手机号可用，验证码将很快发送。"})


@require_POST
def verify_code(request):
    form = VerifyForm(request.POST)
    destination = _safe_next(request.POST.get("next", ""))
    if form.is_valid():
        try:
            account = verify_otp(form.cleaned_data["phone"], form.cleaned_data["code"])
        except (InvalidOtp, LockedOtp, ValueError):
            return render(request, "accounts/login.html", {"phone_form": PhoneForm(initial={"next": destination}), "verify_form": VerifyForm(initial={"next": destination}), "error": "验证码无效或已过期。"})
        login(request, account)
        request.session["session_started_at"] = request.session["session_last_seen_at"] = __import__("django.utils.timezone", fromlist=["now"]).now().isoformat()
        return redirect(destination or "/")
    return render(request, "accounts/login.html", {"phone_form": PhoneForm(initial={"next": destination}), "verify_form": form, "error": "请输入有效的手机号和验证码。"})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/login/")
