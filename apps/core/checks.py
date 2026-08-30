from django.conf import settings
from django.core.checks import Error, Tags, register


UNSAFE_DEVELOPMENT_SECRET_KEY = "unsafe-development-key-change-before-deployment"
UNSAFE_DJANGO_SECRET_KEYS = {
    "",
    "change-me-before-deployment",
    UNSAFE_DEVELOPMENT_SECRET_KEY,
}
UNSAFE_CRYPTO_SECRETS = {"", "change-me-before-deployment", UNSAFE_DEVELOPMENT_SECRET_KEY}


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
    if secret_key in UNSAFE_DJANGO_SECRET_KEYS:
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

    if not settings.DEBUG and getattr(settings, "OTP_FIXED_CODE", None) is not None:
        errors.append(
            Error(
                "A fixed OTP code is not allowed when DEBUG is False.",
                id="phr.E004",
            )
        )

    crypto_secret = getattr(settings, "ACCOUNTS_CRYPTO_SECRET", "")
    if not settings.DEBUG and (
        not getattr(settings, "ACCOUNTS_CRYPTO_SECRET_CONFIGURED", False)
        or crypto_secret in UNSAFE_CRYPTO_SECRETS
    ):
        errors.append(
            Error(
                "A configured non-placeholder account cryptography secret is required.",
                id="phr.E005",
            )
        )

    return errors
