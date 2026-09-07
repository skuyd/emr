"""Explicit boundaries for the isolated synthetic cloud experience."""

from django.conf import settings
from django.http import HttpResponseForbidden


def synthetic_trial_enabled():
    return (
        getattr(settings, "SYNTHETIC_TRIAL", False) is True
        and getattr(settings, "PRODUCTION_DEPLOYMENT", False) is False
        and settings.DEBUG is False
        and getattr(settings, "OTP_PROVIDER", None) == "synthetic_trial"
    )


def trial_environment(request):
    return {"synthetic_trial": synthetic_trial_enabled()}


class SyntheticTrialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        enabled = synthetic_trial_enabled()
        if enabled and request.path.startswith(("/login/first-use/", "/login/forgot-password/")):
            return HttpResponseForbidden("体验环境仅使用已提供的体验账号。", content_type="text/plain; charset=utf-8")
        response = self.get_response(request)
        if enabled:
            response.headers["X-EMR-Environment"] = "synthetic-trial"
        return response
