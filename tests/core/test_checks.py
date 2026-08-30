import pytest
from django.core.checks import Tags, run_checks
from django.test import override_settings

from config.settings import dev as dev_settings


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


@pytest.mark.parametrize("secret_key", ["", "unsafe-development-key-change-before-deployment"])
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
