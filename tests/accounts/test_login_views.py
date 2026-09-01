import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.crypto import hash_phone
from apps.accounts.forms import MfaForm
from apps.accounts.flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
)
from apps.accounts.models import OtpChallenge
from apps.analytics.models import ProductEvent
from apps.patients.services import create_patient_space
from tests.accounts.fakes import FailingSmsProvider, RecordingSmsProvider


@pytest.fixture
def account(db, django_user_model):
    return django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"),
        phone_encrypted="ciphertext",
        password="valid-password",
    )


@pytest.fixture
def provider(monkeypatch):
    sms_provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: sms_provider)
    return sms_provider


def _complete_onboarding(account):
    create_patient_space(
        account,
        "王小明",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "test"},
    )


@pytest.mark.django_db
def test_login_page_has_persistent_password_labels_and_progressive_form_actions(client):
    response = client.get("/login/")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'action="/login/password/"' in content
    assert 'for="id_phone">手机号</label>' in content
    assert 'for="id_password">密码</label>' in content
    assert 'autocomplete="tel"' in content
    assert 'autocomplete="current-password"' in content
    assert 'href="/login/first-use/"' in content
    assert 'href="/login/forgot-password/"' in content
    assert 'href="/privacy/"' in content


@pytest.mark.django_db
def test_generic_login_failure_uses_focusable_summary_without_echoing_identity(client, provider):
    phone = "13900000000"

    response = client.post("/login/password/", {"phone": phone, "password": "wrong"})
    content = response.content.decode()

    assert response.status_code == 400
    assert content.index('class="error-summary"') < content.index("<form")
    assert re.search(
        r'class="error-summary"\s+role="alert"\s+tabindex="-1"',
        content,
    )
    assert 'aria-labelledby="login-errors-title"' in content
    assert "手机号或密码不正确" in content
    assert phone not in content
    assert "field-errors" not in content


@pytest.mark.django_db
def test_login_first_use_link_preserves_a_safe_return_destination(client):
    response = client.get("/login/?next=/records/%3Ftab%3D1")

    assert response.status_code == 200
    assert (
        'href="/login/first-use/?next=/records/%3Ftab%3D1"'
        in response.content.decode()
    )


def test_login_actions_are_post_only_and_legacy_otp_request_route_is_gone(client):
    assert client.get("/login/password/").status_code == 405
    assert client.get("/login/verify/").status_code == 400
    assert client.get("/logout/").status_code == 405
    assert client.post("/login/request-code/", {}).status_code == 404


def test_mfa_form_rejects_non_numeric_six_character_codes():
    assert not MfaForm({"code": "abcdef"}).is_valid()


@pytest.mark.django_db
def test_password_and_mfa_posts_require_csrf(client):
    csrf_client = Client(enforce_csrf_checks=True)

    assert csrf_client.post("/login/password/", {"phone": "13800138000", "password": "wrong"}).status_code == 403
    assert csrf_client.post("/login/verify/", {"code": "123456"}).status_code == 403
    assert csrf_client.post("/logout/").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "phone,password",
    [
        ("13900000000", "wrong"),
        ("13800138000", "wrong"),
        ("not-a-phone", "wrong"),
        ("13800138000", ""),
    ],
)
def test_password_step_uses_one_generic_invalid_credentials_message(client, account, provider, phone, password):
    response = client.post("/login/password/", {"phone": phone, "password": password})

    assert response.status_code == 400
    assert "手机号或密码不正确" in response.content.decode()
    assert OtpChallenge.objects.count() == 0
    assert provider.codes == []
    assert "_auth_user_id" not in client.session
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["inactive", "unusable"])
def test_password_step_keeps_inactive_and_unusable_accounts_indistinguishable(client, account, provider, state):
    if state == "inactive":
        account.is_active = False
        account.save(update_fields=["is_active"])
    else:
        account.set_unusable_password()
        account.save(update_fields=["password"])

    response = client.post("/login/password/", {"phone": "13800138000", "password": "valid-password"})

    assert response.status_code == 400
    assert "手机号或密码不正确" in response.content.decode()
    assert OtpChallenge.objects.count() == 0
    assert provider.codes == []


@pytest.mark.django_db
def test_password_step_keeps_an_exhausted_password_throttle_generic(client, account, provider):
    responses = [
        client.post("/login/password/", {"phone": "13800138000", "password": "wrong"})
        for _ in range(6)
    ]

    assert all(response.status_code == 400 for response in responses)
    assert all("手机号或密码不正确" in response.content.decode() for response in responses)
    assert OtpChallenge.objects.count() == 0
    assert provider.codes == []


@pytest.mark.django_db
def test_password_step_stores_only_server_side_mfa_state_after_provider_accepts(client, account, provider):
    response = client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password", "next": "/records/?tab=1"},
    )

    assert response.status_code == 302
    assert response["Location"] == "/login/verify/"
    assert provider.codes
    assert set(client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]) == {
        "account_id", "challenge_id", "destination", "issued_at"
    }
    assert client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]["destination"] == "/records/?tab=1"
    assert "13800138000" not in str(client.session[SIGN_IN_PENDING_MFA_SESSION_KEY])
    assert "valid-password" not in str(client.session[SIGN_IN_PENDING_MFA_SESSION_KEY])
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_password_step_does_not_store_mfa_state_when_provider_delivery_fails(client, account, monkeypatch):
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: FailingSmsProvider())

    response = client.post("/login/password/", {"phone": "13800138000", "password": "valid-password"})

    assert response.status_code == 400
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_mfa_page_requires_server_side_state_and_uses_one_time_code_form(client, account, provider):
    assert client.get("/login/verify/").status_code == 400

    client.post("/login/password/", {"phone": "13800138000", "password": "valid-password"})
    response = client.get("/login/verify/")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'for="id_code">验证码</label>' in content
    assert 'autocomplete="one-time-code"' in content
    assert 'name="phone"' not in content
    assert 'name="next"' not in content
    assert 'src="/static/js/login.js"' in content


@pytest.mark.django_db
def test_mfa_page_always_offers_neutral_restart_to_login(client, account, provider):
    client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password"},
    )

    response = client.get("/login/verify/")

    assert response.status_code == 200
    assert '<a href="/login/">重新登录</a>' in response.content.decode()


@pytest.mark.django_db
def test_mfa_rejects_missing_or_malformed_server_state_anonymously(client):
    session = client.session
    session[SIGN_IN_PENDING_MFA_SESSION_KEY] = {"challenge_id": 1}
    session.save()

    response = client.post("/login/verify/", {"code": "123456", "phone": "13800138000", "next": "/records/"})

    assert response.status_code == 400
    assert "_auth_user_id" not in client.session
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_wrong_mfa_code_leaves_client_anonymous_and_does_not_trust_posted_identity(client, account, provider):
    client.post("/login/password/", {"phone": "13800138000", "password": "valid-password", "next": "/records/"})

    response = client.post(
        "/login/verify/",
        {"code": "000000", "phone": "13900000000", "account_id": "not-the-account", "next": "//evil.example"},
    )

    assert response.status_code == 400
    assert "_auth_user_id" not in client.session
    assert client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]["destination"] == "/records/"


def _start_pending_mfa(client, account, provider):
    response = client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password", "next": "/records/"},
    )
    assert response.status_code == 302
    challenge_id = client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]["challenge_id"]
    return OtpChallenge.objects.get(pk=challenge_id)


def _assert_failed_mfa_stays_anonymous(client, provider):
    response = client.post("/login/verify/", {"code": provider.last_code, "next": "//evil.example"})

    assert response.status_code == 400
    assert "_auth_user_id" not in client.session
    assert "post_onboarding_next" not in client.session


@pytest.mark.django_db
def test_expired_mfa_challenge_stays_anonymous_without_promoting_posted_next(client, account, provider):
    challenge = _start_pending_mfa(client, account, provider)
    challenge.expires_at = timezone.now() - timedelta(seconds=1)
    challenge.save(update_fields=["expires_at"])

    _assert_failed_mfa_stays_anonymous(client, provider)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_locked_mfa_challenge_stays_anonymous_without_promoting_posted_next(client, account, provider):
    challenge = _start_pending_mfa(client, account, provider)
    challenge.locked_at = timezone.now()
    challenge.save(update_fields=["locked_at"])

    _assert_failed_mfa_stays_anonymous(client, provider)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_replayed_mfa_challenge_stays_anonymous_without_promoting_posted_next(client, account, provider):
    challenge = _start_pending_mfa(client, account, provider)
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at"])

    _assert_failed_mfa_stays_anonymous(client, provider)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_account_mismatched_mfa_challenge_stays_anonymous_without_promoting_posted_next(client, account, provider, django_user_model):
    _start_pending_mfa(client, account, provider)
    other = django_user_model.objects.create(phone_hash="f" * 64, phone_encrypted="other")
    session = client.session
    payload = dict(session[SIGN_IN_PENDING_MFA_SESSION_KEY])
    payload["account_id"] = str(other.pk)
    session[SIGN_IN_PENDING_MFA_SESSION_KEY] = payload
    session.save()

    _assert_failed_mfa_stays_anonymous(client, provider)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_purpose_mismatched_mfa_challenge_stays_anonymous_without_promoting_posted_next(client, account, provider):
    challenge = _start_pending_mfa(client, account, provider)
    challenge.purpose = OtpChallenge.Purpose.FIRST_USE
    challenge.save(update_fields=["purpose"])

    _assert_failed_mfa_stays_anonymous(client, provider)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_mfa_login_rotates_session_honors_safe_next_and_clears_all_flow_state(client, account, provider):
    _complete_onboarding(account)
    session = client.session
    session[ENROLLMENT_PENDING_MFA_SESSION_KEY] = {"unrelated": "state"}
    session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = {"unrelated": "state"}
    session.save()
    client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password", "next": "/records/"},
    )
    before = client.session.session_key
    assert before is not None

    response = client.post("/login/verify/", {"code": provider.last_code, "next": "//evil.example"})

    assert response.status_code == 302
    assert response["Location"] == "/records/"
    assert client.session.session_key != before
    assert client.session["_auth_user_id"] == str(account.pk)
    assert isinstance(client.session["session_started_at"], int)
    assert isinstance(client.session["session_last_seen_at"], int)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    assert ProductEvent.objects.filter(name="login_succeeded").count() == 1


@pytest.mark.django_db
def test_mfa_login_routes_incomplete_account_to_onboarding_before_safe_next(client, account, provider):
    client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password", "next": "/records/"},
    )

    response = client.post("/login/verify/", {"code": provider.last_code})

    assert response["Location"] == "/onboarding/"
    assert client.session["post_onboarding_next"] == "/records/"


@pytest.mark.django_db
def test_mfa_login_without_next_clears_stale_post_onboarding_destination(client, account, provider):
    session = client.session
    session["post_onboarding_next"] = "/stale-safe-destination/"
    session.save()
    client.post("/login/password/", {"phone": "13800138000", "password": "valid-password"})

    response = client.post("/login/verify/", {"code": provider.last_code})

    assert response["Location"] == "/onboarding/"
    assert "post_onboarding_next" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("next_value", ["//evil.example", "https://evil.example/", "/%255Cevil", "/login/"])
def test_password_step_never_preserves_unsafe_next(client, account, provider, next_value):
    response = client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "valid-password", "next": next_value},
    )

    assert response.status_code == 302
    assert client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]["destination"] == "/"
