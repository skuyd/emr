from django.conf import settings
from django.core.checks import run_checks


def test_project_uses_custom_account_model():
    assert settings.AUTH_USER_MODEL == "accounts.Account"


def test_security_middleware_is_enabled():
    names = set(settings.MIDDLEWARE)
    assert "django.middleware.security.SecurityMiddleware" in names
    assert "django.middleware.csrf.CsrfViewMiddleware" in names


def test_django_system_checks_are_clean():
    assert run_checks() == []
