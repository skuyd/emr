from django.test import override_settings
import pytest

from tests.documents.test_detail_viewer import _parsed_document, _patient


pytestmark = pytest.mark.django_db


def test_dynamic_responses_have_strict_default_security_headers(client):
    response = client.get("/login/", secure=True)

    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store, max-age=0"
    assert response["Referrer-Policy"] == "same-origin"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "camera=()" in response["Permissions-Policy"]
    csp = response["Content-Security-Policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "worker-src 'self'",
    ):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response["Strict-Transport-Security"].startswith("max-age=31536000")


def test_cookie_security_defaults_are_explicit(settings):
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_HTTPONLY is True
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"


def test_same_origin_viewer_is_the_only_embeddable_sensitive_html(django_user_model):
    client, patient = _patient(django_user_model, "h")
    document, _first_evidence, _second_evidence = _parsed_document(patient)

    embedded = client.get(f"/records/{document.pk}/viewer/?embed=1")
    detail = client.get(f"/records/{document.pk}/")

    assert "frame-ancestors 'self'" in embedded["Content-Security-Policy"]
    assert embedded["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'none'" in detail["Content-Security-Policy"]


def test_logout_requests_browser_side_sensitive_state_cleanup(django_user_model):
    client, _patient_value = _patient(django_user_model, "i")

    response = client.post("/logout/")

    assert response.status_code == 302
    assert response["Clear-Site-Data"] == '"cache", "storage"'
