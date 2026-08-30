from django.conf import settings
from django.core.checks import Error, Tags, register

from apps.patients.policies import policy_configuration_errors


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

    if policy_configuration_errors():
        errors.append(
            Error(
                "Consent policies must have complete canonical content and matching SHA-256 digests.",
                id="phr.E006",
            )
        )

    notification_secret = getattr(settings, "NOTIFICATIONS_CRYPTO_SECRET", "")
    if not settings.DEBUG and (
        not getattr(settings, "NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED", False)
        or notification_secret in UNSAFE_CRYPTO_SECRETS
    ):
        errors.append(
            Error(
                "A configured non-placeholder notification cryptography secret is required.",
                id="phr.E007",
            )
        )

    if getattr(settings, "WEBPUSH_ENABLED", False):
        subject = getattr(settings, "WEBPUSH_VAPID_SUBJECT", "")
        ttl = getattr(settings, "WEBPUSH_TTL_SECONDS", 0)
        timeout = getattr(settings, "WEBPUSH_TIMEOUT_SECONDS", 0)
        allowed_hosts = getattr(settings, "WEBPUSH_ALLOWED_ENDPOINT_HOSTS", ())
        if (
            not getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "")
            or not getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "")
            or not (subject.startswith("mailto:") or subject.startswith("https://"))
            or type(ttl) is not int
            or not 0 < ttl <= 86400
            or type(timeout) is not int
            or not 0 < timeout <= 30
            or not isinstance(allowed_hosts, (list, tuple))
            or not allowed_hosts
            or any(not isinstance(host, str) or not host or host == "*" for host in allowed_hosts)
        ):
            errors.append(
                Error(
                    "Web Push requires VAPID keys, a valid subject and a bounded TTL.",
                    id="phr.E008",
                )
            )

    return errors
