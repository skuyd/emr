import uuid

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from freezegun import freeze_time

from apps.accounts.authentication import PendingMfa
from apps.accounts.flow_state import (
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    load_pending_mfa,
    store_pending_mfa,
)


def _request():
    request = RequestFactory().get("/login/", HTTP_HOST="testserver")
    SessionMiddleware(lambda request: None).process_request(request)
    request.session.save()
    return request


@freeze_time("2026-08-30 08:00:00")
def test_pending_mfa_stores_and_loads_exact_privacy_safe_payload(db):
    request = _request()
    pending = PendingMfa(account_id=uuid.uuid4(), challenge_id=42)

    store_pending_mfa(request, pending, "/records/?tab=1")

    assert request.session[SIGN_IN_PENDING_MFA_SESSION_KEY] == {
        "account_id": str(pending.account_id),
        "challenge_id": 42,
        "destination": "/records/?tab=1",
        "issued_at": 1788076800,
    }
    assert load_pending_mfa(request).account_id == pending.account_id
    assert load_pending_mfa(request).challenge_id == 42


@pytest.mark.parametrize(
    "payload",
    [
        {"account_id": "not-a-uuid", "challenge_id": 1, "destination": "/", "issued_at": 1788076800},
        {"account_id": str(uuid.uuid4()), "challenge_id": True, "destination": "/", "issued_at": 1788076800},
        {"account_id": str(uuid.uuid4()), "challenge_id": 1, "destination": "//evil.example", "issued_at": 1788076800},
        {"account_id": str(uuid.uuid4()), "challenge_id": 1, "destination": "/", "issued_at": True},
        {"account_id": str(uuid.uuid4()), "challenge_id": 1, "destination": "/", "issued_at": 1788076800, "phone": "13800138000"},
    ],
)
@freeze_time("2026-08-30 08:00:00")
def test_malformed_pending_mfa_is_cleared(db, payload):
    request = _request()
    request.session[SIGN_IN_PENDING_MFA_SESSION_KEY] = payload

    assert load_pending_mfa(request) is None
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in request.session


@freeze_time("2026-08-30 08:05:00")
def test_pending_mfa_at_five_minutes_old_is_cleared(db):
    request = _request()
    request.session[SIGN_IN_PENDING_MFA_SESSION_KEY] = {
        "account_id": str(uuid.uuid4()),
        "challenge_id": 1,
        "destination": "/",
        "issued_at": 1788076800,
    }

    assert load_pending_mfa(request) is None
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in request.session
