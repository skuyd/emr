from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.sessions.models import Session
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import Client, RequestFactory

from apps.accounts.models import AccountSession
from apps.accounts.session import SessionExpiryMiddleware, initialize_session
from apps.accounts.session_registry import revoke_account_sessions


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
def test_idle_session_expires_inclusively_and_encodes_return_path(django_user_model, monkeypatch):
    account = django_user_model.objects.create(phone_hash="c" * 64, phone_encrypted="ciphertext")
    now = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    request = _request_with_session(account, "/records/?page=2", int((now - timedelta(hours=1)).timestamp()), int((now - timedelta(hours=24)).timestamp()))
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: now)

    response = SessionExpiryMiddleware(lambda request: HttpResponse("ok"))(request)
    assert response.status_code == 302
    parsed = urlsplit(response["Location"])
    assert parsed.path == "/login/"
    assert parse_qs(parsed.query)["next"] == ["/records/?page=2"]
    assert "session_started_at" not in request.session
    assert "session_last_seen_at" not in request.session


@pytest.mark.django_db
def test_absolute_session_expires_inclusively_with_recent_activity(django_user_model, monkeypatch):
    account = django_user_model.objects.create(phone_hash="f" * 64, phone_encrypted="ciphertext")
    now = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    request = _request_with_session(account, "/records/?page=2", int((now - timedelta(days=7)).timestamp()), int((now - timedelta(hours=1)).timestamp()))
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: now)
    response = SessionExpiryMiddleware(lambda request: HttpResponse("ok"))(request)
    assert response.status_code == 302


@pytest.mark.django_db
def test_session_just_inside_each_boundary_remains_active(django_user_model, monkeypatch):
    account = django_user_model.objects.create(phone_hash="g" * 64, phone_encrypted="ciphertext")
    now = datetime(2026, 8, 30, tzinfo=datetime_timezone.utc)
    request = _request_with_session(account, "/", int((now - timedelta(days=7) + timedelta(seconds=1)).timestamp()), int((now - timedelta(hours=24) + timedelta(seconds=1)).timestamp()))
    monkeypatch.setattr("apps.accounts.session.timezone.now", lambda: now)
    assert SessionExpiryMiddleware(lambda request: HttpResponse("ok"))(request).status_code == 200


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
def test_logout_is_post_only_flushes_authentication_and_preserves_clear_site_data(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="e" * 64, phone_encrypted="ciphertext")
    client.force_login(account)
    session = client.session
    now = int(datetime.now(datetime_timezone.utc).timestamp())
    session["session_started_at"] = now
    session["session_last_seen_at"] = now
    session.save()

    assert client.get("/logout/").status_code == 405
    response = client.post("/logout/")

    assert response.status_code == 302
    assert response["Location"] == "/login/"
    assert response["Clear-Site-Data"] == '"cache", "storage"'
    assert "_auth_user_id" not in client.session
    assert "session_started_at" not in client.session


@pytest.mark.django_db
def test_revoke_account_sessions_removes_registered_and_decoded_legacy_sessions(django_user_model):
    account = django_user_model.objects.create(phone_hash="a" * 64, phone_encrypted="ciphertext")
    registered = Client()
    registered.force_login(account)
    legacy = Client()
    legacy.force_login(account)
    AccountSession.objects.filter(session_key=legacy.session.session_key).delete()
    keys = {registered.session.session_key, legacy.session.session_key}

    revoked = revoke_account_sessions(account.pk)

    assert revoked == 2
    assert not Session.objects.filter(session_key__in=keys).exists()
    assert not AccountSession.objects.filter(account=account).exists()
