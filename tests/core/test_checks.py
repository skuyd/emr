import pytest
from django.core.checks import Tags, run_checks
from django.test import override_settings

from config.settings import dev as dev_settings


SAFE_PRODUCTION = {
    "PRODUCTION_DEPLOYMENT": True,
    "DEBUG": False,
    "OTP_PROVIDER": "https_gateway",
    "OTP_FIXED_CODE": None,
    "SECRET_KEY": "production-secret-key-with-sufficient-entropy-and-randomness-v1",
    "ACCOUNTS_CRYPTO_SECRET": "production-account-crypto-secret",
    "ACCOUNTS_CRYPTO_SECRET_CONFIGURED": True,
    "NOTIFICATIONS_CRYPTO_SECRET": "production-notification-crypto-secret",
    "NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED": True,
    "TOMBSTONE_HASH_KEY": "production-tombstone-hash-key",
    "TOMBSTONE_HASH_KEY_CONFIGURED": True,
    "TOMBSTONE_SIGNING_KEY": "production-tombstone-signing-key",
    "TOMBSTONE_SIGNING_KEY_CONFIGURED": True,
    "ANALYTICS_HASH_KEY": "production-analytics-hash-key",
    "ANALYTICS_HASH_KEY_CONFIGURED": True,
    "AUDIT_HASH_KEY": "production-audit-hash-key",
    "AUDIT_HASH_KEY_CONFIGURED": True,
    "OPERATIONS_METRICS_TOKEN": "production-metrics-token-with-entropy",
    "OPERATIONS_METRICS_TOKEN_CONFIGURED": True,
    "SESSION_COOKIE_SECURE": True,
    "CSRF_COOKIE_SECURE": True,
    "SECURE_SSL_REDIRECT": True,
    "SECURE_HSTS_SECONDS": 31536000,
    "APP_DOMAIN": "phr.example.test",
    "ACME_EMAIL": "operations@example.test",
    "ALLOWED_HOSTS": ["phr.example.test"],
    "CSRF_TRUSTED_ORIGINS": ["https://phr.example.test"],
    "SMS_GATEWAY_URL": "https://sms.example.test/send",
    "SMS_GATEWAY_API_KEY": "production-sms-api-key",
    "SMS_GATEWAY_SIGNING_SECRET": "production-sms-signing-secret",
    "SMS_GATEWAY_TEMPLATE_ID": "login-template",
    "SMS_GATEWAY_ALLOWED_HOSTS": ["sms.example.test"],
    "SMS_GATEWAY_TIMEOUT_SECONDS": 10,
    "DATABASES": {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "phr"}},
    "CACHES": {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://redis:6379/0",
        }
    },
    "CELERY_BROKER_URL": "redis://redis:6379/0",
    "CELERY_RESULT_BACKEND": "redis://redis:6379/1",
    "DOCUMENT_STORAGE_BACKEND": "s3",
    "DOCUMENT_S3_BUCKET": "private-bucket",
    "DOCUMENT_S3_REGION": "ap-southeast-1",
    "DOCUMENT_S3_ACCESS_KEY_ID": "production-storage-key",
    "DOCUMENT_S3_SECRET_ACCESS_KEY": "production-storage-secret",
    "DOCUMENT_S3_PREFIX": "production",
    "DOCUMENT_S3_ENDPOINT_URL": "https://s3.example.test",
    "DOCUMENT_S3_ALLOWED_HOSTS": ["s3.example.test"],
    "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL": False,
    "PHR_OCR_PROVIDER": "paddle",
    "PHR_OCR_PADDLE_DETECTION_MODEL_DIR": "/models/detection",
    "PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR": "/models/recognition",
    "MIDDLEWARE": [
        "django.middleware.security.SecurityMiddleware",
        "whitenoise.middleware.WhiteNoiseMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
    ],
    "STORAGES": {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    },
}


def phr_security_ids():
    return {error.id for error in run_checks(tags=[Tags.security]) if error.id.startswith("phr.")}


@override_settings(
    DEBUG=False,
    OTP_PROVIDER="console",
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_rejects_console_otp_provider():
    assert "phr.E001" in phr_security_ids()


@pytest.mark.parametrize(
    "secret_key",
    ["", "unsafe-development-key-change-before-deployment", "change-me-before-deployment"],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_rejects_empty_or_development_secret_keys(secret_key):
    with override_settings(SECRET_KEY=secret_key):
        assert "phr.E002" in phr_security_ids()


@pytest.mark.parametrize(
    "cookie_override",
    [
        {"SESSION_COOKIE_SECURE": False},
        {"CSRF_COOKIE_SECURE": False},
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_requires_both_secure_cookie_flags(cookie_override):
    with override_settings(**cookie_override):
        assert "phr.E003" in phr_security_ids()


@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_safe_production_configuration_has_no_phr_security_errors():
    assert phr_security_ids() == set()


def test_development_settings_generate_a_process_local_secret_for_unsafe_inheritance():
    first_secret = dev_settings._development_secret_key(
        "unsafe-development-key-change-before-deployment", configured=False
    )
    second_secret = dev_settings._development_secret_key(
        "unsafe-development-key-change-before-deployment", configured=False
    )

    assert first_secret
    assert first_secret != "unsafe-development-key-change-before-deployment"
    assert first_secret != second_secret
    assert dev_settings._development_secret_key("", configured=True) == ""


def test_development_settings_preserve_explicit_unsafe_secret():
    unsafe_key = "unsafe-development-key-change-before-deployment"

    assert dev_settings._development_secret_key(unsafe_key, configured=True) == unsafe_key


@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE="123456",
    SECRET_KEY="production-secret-key",
    ACCOUNTS_CRYPTO_SECRET="production-crypto-secret",
    ACCOUNTS_CRYPTO_SECRET_CONFIGURED=True,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_rejects_fixed_otp_code():
    assert "phr.E004" in phr_security_ids()


@pytest.mark.parametrize(
    "crypto_secret, configured",
    [
        ("", True),
        ("change-me-before-deployment", True),
        ("unsafe-development-key-change-before-deployment", True),
        ("production-crypto-secret", False),
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE=None,
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_rejects_missing_or_placeholder_crypto_secret(crypto_secret, configured):
    with override_settings(
        ACCOUNTS_CRYPTO_SECRET=crypto_secret,
        ACCOUNTS_CRYPTO_SECRET_CONFIGURED=configured,
    ):
        assert "phr.E005" in phr_security_ids()


@override_settings(CONSENT_POLICIES={"privacy": {"version": "2026-08-30", "digest": "invalid"}})
def test_consent_policy_configuration_requires_complete_canonical_digests():
    assert "phr.E006" in phr_security_ids()


def test_consent_policy_configuration_rejects_digest_that_does_not_match_content(settings):
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "a" * 64

    with override_settings(CONSENT_POLICIES=policies):
        assert "phr.E006" in phr_security_ids()


@pytest.mark.parametrize(
    "crypto_secret, configured",
    [
        ("", True),
        ("change-me-before-deployment", True),
        ("production-notification-secret", False),
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE=None,
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_rejects_missing_notification_crypto_secret(crypto_secret, configured):
    with override_settings(
        NOTIFICATIONS_CRYPTO_SECRET=crypto_secret,
        NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED=configured,
    ):
        assert "phr.E007" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"WEBPUSH_VAPID_PUBLIC_KEY": ""},
        {"WEBPUSH_VAPID_PRIVATE_KEY": ""},
        {"WEBPUSH_VAPID_SUBJECT": "not-a-contact"},
        {"WEBPUSH_TTL_SECONDS": 0},
        {"WEBPUSH_TTL_SECONDS": 86401},
        {"WEBPUSH_TIMEOUT_SECONDS": 0},
        {"WEBPUSH_TIMEOUT_SECONDS": 31},
        {"WEBPUSH_ALLOWED_ENDPOINT_HOSTS": []},
        {"WEBPUSH_ALLOWED_ENDPOINT_HOSTS": ["*"]},
    ],
)
@override_settings(
    WEBPUSH_ENABLED=True,
    WEBPUSH_VAPID_PUBLIC_KEY="public-key",
    WEBPUSH_VAPID_PRIVATE_KEY="private-key",
    WEBPUSH_VAPID_SUBJECT="mailto:operations@example.invalid",
    WEBPUSH_TTL_SECONDS=300,
    WEBPUSH_TIMEOUT_SECONDS=10,
    WEBPUSH_ALLOWED_ENDPOINT_HOSTS=["push.example.test"],
)
def test_enabled_webpush_requires_complete_bounded_configuration(override):
    with override_settings(**override):
        assert "phr.E008" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"TOMBSTONE_HASH_KEY": "", "TOMBSTONE_HASH_KEY_CONFIGURED": True},
        {"TOMBSTONE_HASH_KEY": "production-hash", "TOMBSTONE_HASH_KEY_CONFIGURED": False},
        {"TOMBSTONE_SIGNING_KEY": "change-me-before-deployment", "TOMBSTONE_SIGNING_KEY_CONFIGURED": True},
        {"TOMBSTONE_SIGNING_KEY": "production-signing", "TOMBSTONE_SIGNING_KEY_CONFIGURED": False},
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE=None,
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_requires_external_restore_tombstone_keys(override):
    with override_settings(**override):
        assert "phr.E009" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"ANALYTICS_HASH_KEY": "", "ANALYTICS_HASH_KEY_CONFIGURED": True},
        {"ANALYTICS_HASH_KEY": "production-analytics", "ANALYTICS_HASH_KEY_CONFIGURED": False},
        {"AUDIT_HASH_KEY": "change-me-before-deployment", "AUDIT_HASH_KEY_CONFIGURED": True},
        {"AUDIT_HASH_KEY": "production-audit", "AUDIT_HASH_KEY_CONFIGURED": False},
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE=None,
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_requires_explicit_analytics_and_audit_hash_keys(override):
    with override_settings(**override):
        assert "phr.E010" in phr_security_ids()


@pytest.mark.parametrize(
    "token,configured",
    [
        ("", True),
        ("short", True),
        ("change-me-before-deployment-at-least-32-characters", True),
        ("a-valid-production-metrics-token-value", False),
    ],
)
@override_settings(
    DEBUG=False,
    OTP_PROVIDER="sms",
    OTP_FIXED_CODE=None,
    SECRET_KEY="production-secret-key",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_production_requires_high_entropy_metrics_token(token, configured):
    with override_settings(
        OPERATIONS_METRICS_TOKEN=token,
        OPERATIONS_METRICS_TOKEN_CONFIGURED=configured,
    ):
        assert "phr.E011" in phr_security_ids()


@override_settings(**SAFE_PRODUCTION)
def test_complete_production_contract_has_no_project_security_errors():
    assert phr_security_ids() == set()


@override_settings(**{**SAFE_PRODUCTION, "SECRET_KEY": "too-short-for-production"})
def test_complete_production_contract_rejects_a_short_django_secret():
    assert "phr.E002" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"OTP_PROVIDER": "sms"},
        {"SMS_GATEWAY_URL": "http://sms.example.test/send"},
        {"SMS_GATEWAY_URL": "https://other.example.test/send"},
        {"SMS_GATEWAY_URL": "https://user:pass@sms.example.test/send"},
        {"SMS_GATEWAY_URL": "https://sms.example.test:8443/send"},
        {"SMS_GATEWAY_URL": "https://sms.example.test/send?redirect=true"},
        {"SMS_GATEWAY_API_KEY": "required-api-key"},
        {"SMS_GATEWAY_SIGNING_SECRET": ""},
        {"SMS_GATEWAY_TIMEOUT_SECONDS": 11},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_bounded_allowlisted_sms_gateway(override):
    with override_settings(**override):
        assert "phr.E012" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"DATABASES": {"default": {"ENGINE": "django.db.backends.sqlite3"}}},
        {"CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}},
        {"CELERY_BROKER_URL": ""},
        {"CELERY_RESULT_BACKEND": "memory://"},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_postgres_redis_cache_and_broker(override):
    with override_settings(**override):
        assert "phr.E013" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"DOCUMENT_STORAGE_BACKEND": "local"},
        {"DOCUMENT_S3_BUCKET": ""},
        {"DOCUMENT_S3_ACCESS_KEY_ID": "required-access-key"},
        {"DOCUMENT_S3_ENDPOINT_URL": ""},
        {"DOCUMENT_S3_ENDPOINT_URL": "http://public-object-store.example.test"},
        {"DOCUMENT_S3_ENDPOINT_URL": "https://metadata.internal"},
        {"DOCUMENT_S3_ENDPOINT_URL": "https://user:pass@s3.example.test"},
        {"DOCUMENT_S3_ENDPOINT_URL": "https://s3.example.test/?unsafe=1"},
        {"DOCUMENT_S3_ALLOWED_HOSTS": ["*"]},
        {"DOCUMENT_S3_PREFIX": ""},
        {"DOCUMENT_S3_PREFIX": "../production"},
        {"DOCUMENT_S3_PREFIX": "/shared"},
        {"DOCUMENT_S3_PREFIX": "tenant//objects"},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_private_configured_s3(override):
    with override_settings(**override):
        assert "phr.E014" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"SECURE_SSL_REDIRECT": False},
        {"SECURE_HSTS_SECONDS": 0},
        {"APP_DOMAIN": ""},
        {"APP_DOMAIN": "other.example.test"},
        {"ACME_EMAIL": "required-operations-contact"},
        {"ALLOWED_HOSTS": ["*"]},
        {"CSRF_TRUSTED_ORIGINS": []},
        {"CSRF_TRUSTED_ORIGINS": ["http://phr.example.test"]},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_explicit_https_origin_contract(override):
    with override_settings(**override):
        assert "phr.E015" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"PHR_OCR_PROVIDER": "fixture"},
        {"PHR_OCR_PADDLE_DETECTION_MODEL_DIR": ""},
        {"PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR": ""},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_explicit_offline_ocr_models(override):
    with override_settings(**override):
        assert "phr.E016" in phr_security_ids()


@pytest.mark.parametrize(
    "override",
    [
        {"MIDDLEWARE": ["django.middleware.security.SecurityMiddleware"]},
        {"STORAGES": {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}},
    ],
)
@override_settings(**SAFE_PRODUCTION)
def test_production_requires_whitenoise_manifest_static_contract(override):
    with override_settings(**override):
        assert "phr.E017" in phr_security_ids()
