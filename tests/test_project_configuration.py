from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.checks import run_checks
from django.template.loader import get_template

from config.settings import dev as dev_settings


def test_project_uses_custom_account_model():
    assert settings.AUTH_USER_MODEL == "accounts.Account"


def test_security_middleware_is_enabled():
    names = set(settings.MIDDLEWARE)
    assert "django.middleware.security.SecurityMiddleware" in names
    assert "django.middleware.csrf.CsrfViewMiddleware" in names


def test_django_system_checks_are_clean():
    assert run_checks() == []


def test_development_settings_use_canonical_otp_provider_name():
    # A legitimate local .env may choose a non-console provider; this contract
    # verifies the canonical setting name without coupling import to that value.
    assert isinstance(dev_settings.OTP_PROVIDER, str)
    assert dev_settings.OTP_PROVIDER
    assert not hasattr(dev_settings, "OTP_DELIVERY_BACKEND")


def test_project_static_assets_are_discoverable():
    for asset in (
        "favicon.svg",
        "css/tokens.css",
        "css/components.css",
        "css/public.css",
        "css/app-shell.css",
        "js/login.js",
        "js/app-shell.js",
    ):
        assert finders.find(asset), f"Static asset is not discoverable: {asset}"


def test_shared_component_templates_are_discoverable():
    for template_name in (
        "components/_brand.html",
        "components/_icon.html",
        "components/_status_badge.html",
        "components/_form_errors.html",
        "components/_empty_state.html",
        "components/_record_card.html",
    ):
        assert get_template(template_name)
