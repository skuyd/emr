from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory

from apps.accounts.session import SessionExpiryMiddleware, initialize_session


def _request_with_session(account, path, started=None, seen=None):
    request = RequestFactory().get(path)
    SessionMiddleware(lambda request: None).process_request(request)
    request.session.save()
    request.user = account
    if started is not None:
        request.session["session_started_at"] = started
    if seen is not None:
        request.session["session_last_seen_at"] = seen
    return request


@pytest.mark.django_db
def test_initialize_session_uses_utc_epoch_seconds(django_user_model, monkeypatch):
    account = django_user_model.objects.create(phone_hash="b" * 64, phone_encrypted="ciphertext")
    request = _request_with_session(account, "/")
    moment = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: moment)

    initialize_session(request)
    assert request.session["session_started_at"] == 1788048000
    assert request.session["session_last_seen_at"] == 1788048000


@pytest.mark.django_db
@pytest.mark.parametrize("duration", [timedelta(hours=24), timedelta(days=7)])
def test_session_expiry_boundaries_are_inclusive_and_encode_return_path(django_user_model, monkeypatch, duration):
    account = django_user_model.objects.create(phone_hash="c" * 64, phone_encrypted="ciphertext")
    now = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    timestamp = int((now - duration).timestamp())
    request = _request_with_session(account, "/records/?page=2", timestamp, timestamp)
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: now)

    response = SessionExpiryMiddleware(lambda request: HttpResponse("ok"))(request)
    assert response.status_code == 302
    parsed = urlsplit(response["Location"])
    assert parsed.path == "/login/"
    assert parse_qs(parsed.query)["next"] == ["/records/?page=2"]
    assert "session_started_at" not in request.session
    assert "session_last_seen_at" not in request.session


@pytest.mark.django_db
def test_active_session_is_initialized_and_last_seen_is_updated(django_user_model, monkeypatch):
    account = django_user_model.objects.create(phone_hash="d" * 64, phone_encrypted="ciphertext")
    now = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    request = _request_with_session(account, "/")
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: now)

    response = SessionExpiryMiddleware(lambda request: HttpResponse("ok"))(request)
    assert response.status_code == 200
    assert request.session["session_started_at"] == int(now.timestamp())
    assert request.session["session_last_seen_at"] == int(now.timestamp())


@pytest.mark.django_db
def test_logout_flushes_epoch_session_data(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="e" * 64, phone_encrypted="ciphertext")
    client.force_login(account)
    session = client.session
    session["session_started_at"] = 1
    session["session_last_seen_at"] = 1
    session.save()

    response = client.post("/logout/")
    assert response.status_code == 302
    assert "session_started_at" not in client.session
