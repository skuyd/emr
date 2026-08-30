from django.conf import settings
from django.core.checks import Error, Tags, register


UNSAFE_DEVELOPMENT_SECRET_KEY = "unsafe-development-key-change-before-deployment"


@register(Tags.security)
def check_project_security_settings(app_configs, **kwargs):
    errors = []

    if not settings.DEBUG and getattr(settings, "OTP_PROVIDER", None) == "console":
        errors.append(
            Error(
                "Console OTP delivery is not allowed when DEBUG is False.",
                id="phr.E001",
            )
        )

    secret_key = getattr(settings._wrapped, "SECRET_KEY", "")
    if not secret_key or secret_key == UNSAFE_DEVELOPMENT_SECRET_KEY:
        errors.append(
            Error(
                "A non-development SECRET_KEY is required.",
                id="phr.E002",
            )
        )

    if not settings.DEBUG and (
        settings.SESSION_COOKIE_SECURE is not True
        or settings.CSRF_COOKIE_SECURE is not True
    ):
        errors.append(
            Error(
                "Secure session and CSRF cookies are required when DEBUG is False.",
                id="phr.E003",
            )
        )

    return errors
