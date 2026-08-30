from urllib.parse import urlencode

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone


IDLE_TIMEOUT_SECONDS = 24 * 60 * 60
ABSOLUTE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60


def epoch_seconds(now=None):
    return int((now or timezone.now()).timestamp())


def initialize_session(request, now=None):
    timestamp = epoch_seconds(now)
    request.session["session_started_at"] = timestamp
    request.session["session_last_seen_at"] = timestamp


def _login_redirect_with_next(request):
    return redirect(f"{reverse('accounts:login')}?{urlencode({'next': request.get_full_path()})}")


class SessionExpiryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = epoch_seconds()
            started = request.session.get("session_started_at")
            seen = request.session.get("session_last_seen_at")
            if not isinstance(started, int) or not isinstance(seen, int):
                initialize_session(request)
                return self.get_response(request)
            if now - started >= ABSOLUTE_TIMEOUT_SECONDS or now - seen >= IDLE_TIMEOUT_SECONDS:
                logout(request)
                request.session.flush()
                return _login_redirect_with_next(request)
            request.session["session_last_seen_at"] = now
        return self.get_response(request)
