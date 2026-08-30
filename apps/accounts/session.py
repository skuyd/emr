from datetime import datetime, timedelta

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils import timezone


class SessionExpiryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = timezone.now()
            started = request.session.get("session_started_at")
            seen = request.session.get("session_last_seen_at")
            try:
                expired = now >= datetime.fromisoformat(started) + timedelta(days=7) or now >= datetime.fromisoformat(seen) + timedelta(hours=24)
            except (TypeError, ValueError):
                expired = True
            if expired:
                logout(request)
                return redirect("/login/?next=" + request.get_full_path())
            request.session["session_last_seen_at"] = now.isoformat()
        return self.get_response(request)
