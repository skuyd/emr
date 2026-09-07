import re

from django.conf import settings
from django.core.checks import Error, Tags, register

from apps.accounts.providers import HttpsSmsGatewayProvider, SmsGatewayUnavailable
from apps.core.trial import synthetic_trial_enabled
from apps.documents.backends import valid_s3_endpoint, valid_s3_prefix
from apps.patients.policies import policy_configuration_errors


UNSAFE_DEVELOPMENT_SECRET_KEY = "unsafe-development-key-change-before-deployment"
UNSAFE_DJANGO_SECRET_KEYS = {
    "",
    "change-me-before-deployment",
    UNSAFE_DEVELOPMENT_SECRET_KEY,
}
UNSAFE_CRYPTO_SECRETS = {"", "change-me-before-deployment", UNSAFE_DEVELOPMENT_SECRET_KEY}
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _missing_or_placeholder(value):
    return not isinstance(value, str) or not value or value.startswith(("change-me", "required-"))


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
    if (
        not isinstance(secret_key, str)
        or secret_key in UNSAFE_DJANGO_SECRET_KEYS
        or (getattr(settings, "PRODUCTION_DEPLOYMENT", False) and len(secret_key) < 50)
    ):
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

    if (
        settings.DEBUG is not True
        and getattr(settings, "OTP_FIXED_CODE", None) is not None
        and not synthetic_trial_enabled()
    ):
        errors.append(
            Error(
                "A fixed OTP code requires DEBUG or an explicit nonproduction synthetic trial.",
                id="phr.E004",
            )
        )

    if settings.DEBUG is not True and getattr(settings, "OTP_PROVIDER", None) == "development":
        errors.append(
            Error(
                "Development OTP delivery is not allowed when DEBUG is False.",
                id="phr.E018",
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

    tombstone_keys = (
        (
            getattr(settings, "TOMBSTONE_HASH_KEY", ""),
            getattr(settings, "TOMBSTONE_HASH_KEY_CONFIGURED", False),
        ),
        (
            getattr(settings, "TOMBSTONE_SIGNING_KEY", ""),
            getattr(settings, "TOMBSTONE_SIGNING_KEY_CONFIGURED", False),
        ),
    )
    if not settings.DEBUG and any(
        not configured or value in UNSAFE_CRYPTO_SECRETS for value, configured in tombstone_keys
    ):
        errors.append(
            Error(
                "Configured non-placeholder deletion tombstone keys are required.",
                id="phr.E009",
            )
        )

    analytics_audit_keys = (
        (
            getattr(settings, "ANALYTICS_HASH_KEY", ""),
            getattr(settings, "ANALYTICS_HASH_KEY_CONFIGURED", False),
        ),
        (
            getattr(settings, "AUDIT_HASH_KEY", ""),
            getattr(settings, "AUDIT_HASH_KEY_CONFIGURED", False),
        ),
    )
    if not settings.DEBUG and any(
        not configured or value in UNSAFE_CRYPTO_SECRETS
        for value, configured in analytics_audit_keys
    ):
        errors.append(
            Error(
                "Configured non-placeholder analytics and audit hash keys are required.",
                id="phr.E010",
            )
        )

    metrics_token = getattr(settings, "OPERATIONS_METRICS_TOKEN", "")
    if not settings.DEBUG and (
        not getattr(settings, "OPERATIONS_METRICS_TOKEN_CONFIGURED", False)
        or not isinstance(metrics_token, str)
        or len(metrics_token) < 32
        or metrics_token.startswith("change-me")
    ):
        errors.append(
            Error(
                "A configured high-entropy operations metrics token is required.",
                id="phr.E011",
            )
        )

    if getattr(settings, "PRODUCTION_DEPLOYMENT", False):
        try:
            HttpsSmsGatewayProvider(
                getattr(settings, "SMS_GATEWAY_URL", ""),
                getattr(settings, "SMS_GATEWAY_API_KEY", ""),
                getattr(settings, "SMS_GATEWAY_SIGNING_SECRET", ""),
                getattr(settings, "SMS_GATEWAY_TEMPLATE_ID", ""),
                getattr(settings, "SMS_GATEWAY_ALLOWED_HOSTS", ()),
                timeout=getattr(settings, "SMS_GATEWAY_TIMEOUT_SECONDS", 0),
            )
        except SmsGatewayUnavailable:
            sms_configuration_valid = False
        else:
            sms_configuration_valid = True
        if (
            getattr(settings, "OTP_PROVIDER", "") != "https_gateway"
            or not sms_configuration_valid
            or any(
                _missing_or_placeholder(getattr(settings, name, ""))
                for name in (
                    "SMS_GATEWAY_API_KEY",
                    "SMS_GATEWAY_SIGNING_SECRET",
                    "SMS_GATEWAY_TEMPLATE_ID",
                )
            )
        ):
            errors.append(
                Error(
                    "Production requires a bounded allowlisted HTTPS SMS gateway.",
                    id="phr.E012",
                )
            )

        database_engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
        cache_backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        broker_url = getattr(settings, "CELERY_BROKER_URL", "")
        result_backend = getattr(settings, "CELERY_RESULT_BACKEND", "")
        if (
            "postgresql" not in database_engine
            or "django_redis" not in cache_backend
            or not broker_url.startswith(("redis://", "rediss://"))
            or not result_backend.startswith(("redis://", "rediss://"))
        ):
            errors.append(
                Error(
                    "Production requires PostgreSQL and Redis-backed cache/Celery services.",
                    id="phr.E013",
                )
            )

        storage_values = (
            getattr(settings, "DOCUMENT_S3_BUCKET", ""),
            getattr(settings, "DOCUMENT_S3_REGION", ""),
            getattr(settings, "DOCUMENT_S3_ACCESS_KEY_ID", ""),
            getattr(settings, "DOCUMENT_S3_SECRET_ACCESS_KEY", ""),
        )
        endpoint = getattr(settings, "DOCUMENT_S3_ENDPOINT_URL", "")
        storage_prefix = getattr(settings, "DOCUMENT_S3_PREFIX", "")
        endpoint_is_safe = valid_s3_endpoint(
            endpoint,
            getattr(settings, "DOCUMENT_S3_ALLOWED_HOSTS", ()),
            allow_insecure=getattr(settings, "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL", False),
        )
        if (
            getattr(settings, "DOCUMENT_STORAGE_BACKEND", "") != "s3"
            or any(_missing_or_placeholder(value) for value in storage_values)
            or not valid_s3_prefix(storage_prefix)
            or not endpoint
            or not endpoint_is_safe
        ):
            errors.append(
                Error(
                    "Production requires configured private S3 storage and a safe endpoint.",
                    id="phr.E014",
                )
            )

        allowed_hosts = getattr(settings, "ALLOWED_HOSTS", ())
        trusted_origins = getattr(settings, "CSRF_TRUSTED_ORIGINS", ())
        app_domain = getattr(settings, "APP_DOMAIN", "")
        acme_email = getattr(settings, "ACME_EMAIL", "")
        if (
            settings.SECURE_SSL_REDIRECT is not True
            or type(settings.SECURE_HSTS_SECONDS) is not int
            or settings.SECURE_HSTS_SECONDS < 31536000
            or not isinstance(allowed_hosts, (list, tuple))
            or not allowed_hosts
            or "*" in allowed_hosts
            or not isinstance(trusted_origins, (list, tuple))
            or not trusted_origins
            or any(not origin.startswith("https://") for origin in trusted_origins)
            or not isinstance(app_domain, str)
            or _missing_or_placeholder(app_domain)
            or _HOSTNAME_PATTERN.fullmatch(app_domain) is None
            or app_domain not in allowed_hosts
            or f"https://{app_domain}" not in trusted_origins
            or not isinstance(acme_email, str)
            or _missing_or_placeholder(acme_email)
            or _EMAIL_PATTERN.fullmatch(acme_email) is None
        ):
            errors.append(
                Error(
                    "Production HTTPS hosts, HSTS and trusted origins must be explicit.",
                    id="phr.E015",
                )
            )

        if (
            getattr(settings, "PHR_OCR_PROVIDER", "") != "paddle"
            or not getattr(settings, "PHR_OCR_PADDLE_DETECTION_MODEL_DIR", "")
            or not getattr(settings, "PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR", "")
        ):
            errors.append(
                Error(
                    "Production OCR must use explicit offline Paddle model directories.",
                    id="phr.E016",
                )
            )

        middleware = list(settings.MIDDLEWARE)
        static_backend = settings.STORAGES.get("staticfiles", {}).get("BACKEND", "")
        try:
            whitenoise_position = middleware.index("whitenoise.middleware.WhiteNoiseMiddleware")
            security_position = middleware.index("django.middleware.security.SecurityMiddleware")
        except ValueError:
            whitenoise_position = security_position = -1
        if (
            whitenoise_position != security_position + 1
            or static_backend != "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ):
            errors.append(
                Error(
                    "Production static files require WhiteNoise compressed manifest storage.",
                    id="phr.E017",
                )
            )

    return errors
