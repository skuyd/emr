from datetime import timedelta
import re
from uuid import uuid4

import pytest
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.crypto import hash_phone
from apps.accounts.flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    VERIFIED_PHONE_SESSION_KEY,
)
from apps.accounts.models import AccountSession, OtpChallenge
from tests.accounts.fakes import FailingSmsProvider, RecordingSmsProvider


RESET_STATUS = "如果该手机号可用，我们已发送验证码，请按页面提示继续。"
NEW_PASSWORD = "New strong passphrase 2026"
VERIFIED_PASSWORD_RESET_SESSION_KEY = "verified_password_reset"


@pytest.fixture(autouse=True)
def clear_otp_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def active_account(db, django_user_model):
    return django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"),
        phone_encrypted="ciphertext",
        password="Old strong passphrase 2026",
    )


@pytest.fixture
def provider(monkeypatch):
    sms_provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: sms_provider)
    return sms_provider


def start_password_reset(client, provider, phone="13800138000"):
    response = client.post("/login/forgot-password/", {"phone": phone})
    assert response.status_code == 200
    assert response.context["status"] == RESET_STATUS
    assert provider.last_purpose == OtpChallenge.Purpose.PASSWORD_RESET
    return OtpChallenge.objects.get(pk=client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]["challenge_id"])


def verify_password_reset(client, provider):
    response = client.post("/login/forgot-password/verify/", {"code": provider.last_code})
    assert response.status_code == 302
    assert response["Location"] == "/login/forgot-password/new-password/"


def complete_password_reset(client, provider, password=NEW_PASSWORD):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)
    return client.post(
        "/login/forgot-password/new-password/",
        {"password": password, "password_confirm": password},
    )


@pytest.mark.django_db
def test_reset_request_is_account_neutral_for_active_missing_inactive_and_invalid_phones(
    client, active_account, provider, django_user_model
):
    django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613700137000"),
        phone_encrypted="inactive",
        password="Inactive strong passphrase 2026",
        is_active=False,
    )

    responses = [
        client.post("/login/forgot-password/", {"phone": phone})
        for phone in ("13800138000", "13900000000", "13700137000", "not-a-phone", "")
    ]

    assert all(response.status_code == 200 for response in responses)
    assert all(response.context["status"] == RESET_STATUS for response in responses)
    assert all(not response.context["form"].is_bound for response in responses)
    visible_responses = {
        re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b'name="csrfmiddlewaretoken"', response.content)
        for response in responses
    }
    assert len(visible_responses) == 1
    for response, phone in zip(responses, ("13800138000", "13900000000", "13700137000", "not-a-phone", "")):
        if phone:
            assert phone not in response.content.decode()
    assert OtpChallenge.objects.count() == 1
    assert OtpChallenge.objects.get().account == active_account


@pytest.mark.django_db
def test_reset_request_keeps_provider_failure_neutral_and_does_not_store_pending_state(
    client, active_account, monkeypatch
):
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: FailingSmsProvider())

    response = client.post("/login/forgot-password/", {"phone": "13800138000"})

    assert response.status_code == 200
    assert response.context["status"] == RESET_STATUS
    assert "13800138000" not in response.content.decode()
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    assert not OtpChallenge.objects.filter(
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
    ).exists()


@pytest.mark.django_db
def test_reset_forms_use_explicit_routes_require_csrf_and_work_without_javascript(client):
    csrf_client = Client(enforce_csrf_checks=True)

    request_page = client.get("/login/forgot-password/")
    assert request_page.status_code == 200
    assert 'action="/login/forgot-password/"' in request_page.content.decode()
    assert "<script" not in request_page.content.decode()
    assert csrf_client.post("/login/forgot-password/", {"phone": "13800138000"}).status_code == 403
    assert csrf_client.post("/login/forgot-password/verify/", {"code": "123456"}).status_code == 403
    assert csrf_client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    ).status_code == 403


@pytest.mark.django_db
def test_reset_pending_and_verified_states_are_strict_separate_and_contain_no_secrets(
    client, active_account, provider
):
    start_password_reset(client, provider)
    pending = client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]

    assert set(pending) == {"account_id", "challenge_id", "issued_at"}
    assert pending["account_id"] == str(active_account.pk)
    assert "13800138000" not in str(pending)
    assert provider.last_code not in str(pending)
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session

    verify_password_reset(client, provider)
    verified = client.session[VERIFIED_PASSWORD_RESET_SESSION_KEY]
    assert set(verified) == {"account_id", "challenge_id", "verified_at"}
    assert verified["account_id"] == str(active_account.pk)
    assert verified["challenge_id"] == pending["challenge_id"]
    assert "13800138000" not in str(verified)
    assert provider.last_code not in str(verified)


@pytest.mark.django_db
def test_wrong_reset_code_is_retryable_but_terminal_challenge_failure_clears_flow(
    client, active_account, provider
):
    challenge = start_password_reset(client, provider)

    wrong = client.post("/login/forgot-password/verify/", {"code": "000000"})
    assert wrong.status_code == 400
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY in client.session
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session

    challenge.purpose = OtpChallenge.Purpose.SIGN_IN
    challenge.save(update_fields=["purpose"])
    terminal = client.post("/login/forgot-password/verify/", {"code": provider.last_code})
    assert terminal.status_code == 400
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"account_id": "not-a-uuid", "challenge_id": 1, "issued_at": 1},
        {"account_id": str(uuid4()), "challenge_id": True, "issued_at": 1},
        {"account_id": str(uuid4()), "challenge_id": 1, "issued_at": True},
        {"account_id": str(uuid4()), "challenge_id": 1, "issued_at": 1, "phone": "13800138000"},
    ],
)
def test_malformed_reset_pending_state_is_rejected_and_cleared(client, payload):
    session = client.session
    session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = payload
    session.save()

    response = client.post("/login/forgot-password/verify/", {"code": "123456"})

    assert response.status_code == 400
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_expired_verified_reset_state_cannot_reach_password_submission(
    client, active_account, provider
):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)
    session = client.session
    verified = dict(session[VERIFIED_PASSWORD_RESET_SESSION_KEY])
    verified["verified_at"] -= 300
    session[VERIFIED_PASSWORD_RESET_SESSION_KEY] = verified
    session.save()

    response = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session
    active_account.refresh_from_db()
    assert active_account.check_password("Old strong passphrase 2026")


@pytest.mark.django_db
@override_settings(
    AUTH_PASSWORD_VALIDATORS=[
        {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 20}},
    ]
)
def test_new_password_requires_exact_confirmation_and_configured_django_validators(
    client, active_account, provider
):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)

    mismatch = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": "different passphrase"},
    )
    weak = client.post(
        "/login/forgot-password/new-password/",
        {"password": "short", "password_confirm": "short"},
    )

    assert mismatch.status_code == 400
    assert mismatch.context["form"].errors["password_confirm"]
    assert weak.status_code == 400
    assert weak.context["form"].errors["password"]
    active_account.refresh_from_db()
    assert active_account.check_password("Old strong passphrase 2026")


@pytest.mark.django_db
def test_successful_reset_revokes_registered_legacy_and_current_browser_sessions(
    client, active_account, provider
):
    registered = Client()
    registered.force_login(active_account)
    legacy = Client()
    legacy.force_login(active_account)
    AccountSession.objects.filter(session_key=legacy.session.session_key).delete()
    client.force_login(active_account)
    old_keys = {client.session.session_key, registered.session.session_key, legacy.session.session_key}
    assert AccountSession.objects.filter(account=active_account).count() == 2

    response = complete_password_reset(client, provider)

    assert response.status_code == 302
    assert response["Location"] == "/login/?password-reset=complete"
    assert not Session.objects.filter(session_key__in=old_keys).exists()
    assert not AccountSession.objects.filter(account=active_account).exists()
    assert "_auth_user_id" not in client.session
    assert client.get("/").status_code == 302
    assert registered.get("/").status_code == 302
    assert legacy.get("/").status_code == 302


@pytest.mark.django_db
def test_successful_reset_flushes_every_authentication_flow_key_and_requires_fresh_sign_in_mfa(
    client, active_account, provider
):
    session = client.session
    session[SIGN_IN_PENDING_MFA_SESSION_KEY] = {"stale": True}
    session[ENROLLMENT_PENDING_MFA_SESSION_KEY] = {"stale": True}
    session[VERIFIED_PHONE_SESSION_KEY] = {"stale": True}
    session.save()

    response = complete_password_reset(client, provider)

    assert response["Location"] == "/login/?password-reset=complete"
    assert not any(
        key in client.session
        for key in (
            SIGN_IN_PENDING_MFA_SESSION_KEY,
            ENROLLMENT_PENDING_MFA_SESSION_KEY,
            PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
            VERIFIED_PHONE_SESSION_KEY,
            VERIFIED_PASSWORD_RESET_SESSION_KEY,
        )
    )
    assert "_auth_user_id" not in client.session
    assert client.post(
        "/login/password/", {"phone": "13800138000", "password": "Old strong passphrase 2026"}
    ).status_code == 400
    fresh_login = client.post(
        "/login/password/", {"phone": "13800138000", "password": NEW_PASSWORD}
    )
    assert fresh_login.status_code == 302
    assert fresh_login["Location"] == "/login/verify/"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_verified_reset_ticket_is_one_time_even_if_copied_to_another_session(
    client, active_account, provider
):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)
    pending = dict(client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY])
    verified = dict(client.session[VERIFIED_PASSWORD_RESET_SESSION_KEY])
    replay = Client()
    replay_session = replay.session
    replay_session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = pending
    replay_session[VERIFIED_PASSWORD_RESET_SESSION_KEY] = verified
    replay_session.save()

    first = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )
    second = replay.post(
        "/login/forgot-password/new-password/",
        {"password": "Another strong passphrase 2026", "password_confirm": "Another strong passphrase 2026"},
    )

    assert first.status_code == 302
    assert second.status_code == 400
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in replay.session
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in replay.session
    active_account.refresh_from_db()
    assert active_account.check_password(NEW_PASSWORD)


@pytest.mark.django_db
def test_login_page_shows_only_generic_reset_completion_status(client):
    response = client.get("/login/?password-reset=complete")

    assert response.status_code == 200
    assert response.context["status"]
    assert "13800138000" not in response.content.decode()
