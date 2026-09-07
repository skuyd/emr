from datetime import timedelta
import re
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.authentication import complete_password_login
from apps.accounts.crypto import hash_ip, hash_phone
from apps.accounts.flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
    VERIFIED_PHONE_SESSION_KEY,
)
from apps.accounts.models import AccountSession, OtpChallenge
from apps.accounts.services import LockedOtp, ThrottledOtp, request_otp
from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries
from apps.patients.services import create_patient_space
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
    drain_sms_worker(provider)
    assert response.status_code == 302
    assert response["Location"] == "/login/forgot-password/verify/"
    verification = client.get(response["Location"])
    assert verification.status_code == 200
    assert verification.context["status"] == RESET_STATUS
    assert provider.last_purpose == OtpChallenge.Purpose.PASSWORD_RESET
    return OtpChallenge.objects.get(pk=client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]["challenge_id"])


def drain_sms_worker(provider):
    for job_id in due_sms_deliveries():
        deliver_sms_job(job_id, provider=provider)


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
def test_reset_verification_page_always_offers_restart_link(client, active_account, provider):
    start_password_reset(client, provider)

    response = client.get("/login/forgot-password/verify/")

    assert response.status_code == 200
    assert '<a href="/login/forgot-password/">重新开始</a>' in response.content.decode()


@pytest.mark.django_db
def test_reset_password_page_always_offers_restart_link(client, active_account, provider):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)

    response = client.get("/login/forgot-password/new-password/")

    assert response.status_code == 200
    assert '<a href="/login/forgot-password/">重新开始</a>' in response.content.decode()


@pytest.mark.django_db
def test_reset_password_page_has_concise_neutral_context_copy(client, active_account, provider):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)

    response = client.get("/login/forgot-password/new-password/")

    assert response.status_code == 200
    assert "请设置一个新的登录密码。" in response.content.decode()


def _scrub_csrf(body):
    return re.sub(
        rb'name="csrfmiddlewaretoken" value="[^"]+"',
        b'name="csrfmiddlewaretoken"',
        body,
    )


def _session_cookie_security(response):
    cookie = response.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie is None:
        return None
    return (
        bool(cookie["secure"]),
        bool(cookie["httponly"]),
        cookie["samesite"],
        cookie["path"],
    )


def _assert_uniform_secure_session_cookie(responses):
    assert {_session_cookie_security(response) for response in responses} == {
        (True, True, "Lax", "/")
    }


def _assert_no_session_cookie(responses):
    assert {_session_cookie_security(response) for response in responses} == {None}


@pytest.mark.django_db
def test_reset_verification_http_lifecycle_is_neutral_after_cross_session_supersession(
    active_account, provider, django_user_model, monkeypatch
):
    inactive_phone = "13700137000"
    django_user_model.objects.create_user(
        phone_hash=hash_phone(f"+86{inactive_phone}"),
        phone_encrypted="inactive",
        password="Inactive strong passphrase 2026",
        is_active=False,
    )
    superseded = Client()
    latest_real = Client()
    terminal_real = Client()
    missing = Client()
    inactive = Client()
    expired = Client()
    absent = Client()

    first = superseded.post(
        "/login/forgot-password/", {"phone": "13800138000"}
    )
    drain_sms_worker(provider)
    assert first.status_code == 302
    first_challenge = OtpChallenge.objects.get(
        pk=superseded.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]["challenge_id"]
    )
    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
    second = latest_real.post(
        "/login/forgot-password/", {"phone": "13800138000"}
    )
    drain_sms_worker(provider)
    assert second.status_code == 302
    latest_real_code = provider.last_code
    first_challenge.refresh_from_db()
    assert first_challenge.locked_at is not None
    assert superseded.session.session_key != latest_real.session.session_key

    assert missing.post(
        "/login/forgot-password/", {"phone": "13900139000"}
    ).status_code == 302
    assert inactive.post(
        "/login/forgot-password/", {"phone": inactive_phone}
    ).status_code == 302
    terminal_phone = "13500135000"
    django_user_model.objects.create_user(
        phone_hash=hash_phone(f"+86{terminal_phone}"),
        phone_encrypted="terminal",
        password="Terminal strong passphrase 2026",
    )
    assert terminal_real.post(
        "/login/forgot-password/", {"phone": terminal_phone}
    ).status_code == 302
    assert expired.post(
        "/login/forgot-password/", {"phone": "13600136000"}
    ).status_code == 302
    drain_sms_worker(provider)
    expired_session = expired.session
    expired_payload = dict(
        expired_session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]
    )
    expired_payload["issued_at"] -= 300
    expired_session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = expired_payload
    expired_session.save()

    wrong_code = next(
        candidate
        for candidate in ("000000", "000001", "000002", "000003")
        if candidate not in provider.codes
    )
    indistinguishable = (
        superseded,
        latest_real,
        terminal_real,
        missing,
        inactive,
        expired,
        absent,
    )

    first_failures = [
        reset_client.post(
            "/login/forgot-password/verify/", {"code": wrong_code}
        )
        for reset_client in indistinguishable
    ]
    assert {response.status_code for response in first_failures} == {400}
    assert all("Location" not in response for response in first_failures)
    assert len({_scrub_csrf(response.content) for response in first_failures}) == 1
    _assert_uniform_secure_session_cookie(first_failures)

    first_follow_up_gets = [
        reset_client.get("/login/forgot-password/verify/")
        for reset_client in indistinguishable
    ]
    assert {response.status_code for response in first_follow_up_gets} == {200}
    assert len(
        {_scrub_csrf(response.content) for response in first_follow_up_gets}
    ) == 1
    _assert_no_session_cookie(first_follow_up_gets)

    repeated_failures = [
        reset_client.post(
            "/login/forgot-password/verify/", {"code": wrong_code}
        )
        for reset_client in indistinguishable
    ]
    assert {response.status_code for response in repeated_failures} == {400}
    assert all("Location" not in response for response in repeated_failures)
    assert len({_scrub_csrf(response.content) for response in repeated_failures}) == 1
    _assert_uniform_secure_session_cookie(repeated_failures)
    repeated_gets = [
        reset_client.get("/login/forgot-password/verify/")
        for reset_client in indistinguishable
    ]
    assert {response.status_code for response in repeated_gets} == {200}
    assert len({_scrub_csrf(response.content) for response in repeated_gets}) == 1
    _assert_no_session_cookie(repeated_gets)
    assert all(
        VERIFIED_PASSWORD_RESET_SESSION_KEY not in reset_client.session
        for reset_client in indistinguishable
    )

    terminal_clients = (
        superseded,
        terminal_real,
        missing,
        inactive,
        expired,
        absent,
    )
    for _attempt in range(3, 6):
        terminal_failures = [
            reset_client.post(
                "/login/forgot-password/verify/", {"code": wrong_code}
            )
            for reset_client in terminal_clients
        ]
        assert {response.status_code for response in terminal_failures} == {400}
        assert len(
            {_scrub_csrf(response.content) for response in terminal_failures}
        ) == 1
        _assert_uniform_secure_session_cookie(terminal_failures)
    assert all(
        PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in reset_client.session
        for reset_client in terminal_clients
    )
    terminal_gets = [
        reset_client.get("/login/forgot-password/verify/")
        for reset_client in terminal_clients
    ]
    assert {response.status_code for response in terminal_gets} == {200}
    assert len({_scrub_csrf(response.content) for response in terminal_gets}) == 1
    _assert_no_session_cookie(terminal_gets)

    decoy_rejected = missing.post(
        "/login/forgot-password/verify/", {"code": latest_real_code}
    )
    real_verified = latest_real.post(
        "/login/forgot-password/verify/", {"code": latest_real_code}
    )
    assert decoy_rejected.status_code == 400
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in missing.session
    assert real_verified.status_code == 302
    assert real_verified["Location"] == "/login/forgot-password/new-password/"
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY in latest_real.session


@pytest.mark.django_db
def test_safe_reset_destination_survives_visible_link_state_completion_and_fresh_login(
    client, active_account, provider, monkeypatch
):
    destination = "/records/?source=reset-return"
    create_patient_space(
        active_account,
        "重置返回验收",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "test"},
    )

    login_page = client.get(f"/login/?{urlencode({'next': destination})}")
    assert login_page.status_code == 200
    assert (
        'href="/login/forgot-password/?next=/records/%3Fsource%3Dreset-return"'
        in login_page.content.decode()
    )
    forgot_page = client.get(
        f"/login/forgot-password/?{urlencode({'next': destination})}"
    )
    assert forgot_page.status_code == 200
    assert forgot_page.context["form"]["next"].value() == destination

    requested = client.post(
        "/login/forgot-password/",
        {"phone": "13800138000", "next": destination},
    )
    drain_sms_worker(provider)
    assert requested.status_code == 302
    pending = client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]
    assert set(pending) == {
        "account_id",
        "challenge_id",
        "destination",
        "issued_at",
    }
    assert pending["destination"] == destination

    verified_response = client.post(
        "/login/forgot-password/verify/", {"code": provider.last_code}
    )
    assert verified_response.status_code == 302
    verified = client.session[VERIFIED_PASSWORD_RESET_SESSION_KEY]
    assert set(verified) == {
        "account_id",
        "challenge_id",
        "destination",
        "verified_at",
    }
    assert verified["destination"] == destination

    completed = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )
    assert completed.status_code == 302
    assert completed["Location"] == (
        "/login/?password-reset=complete&next=%2Frecords%2F%3Fsource%3Dreset-return"
    )
    fresh_login_page = client.get(completed["Location"])
    assert fresh_login_page.status_code == 200
    assert fresh_login_page.context["form"]["next"].value() == destination

    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
    password_step = client.post(
        "/login/password/",
        {
            "phone": "13800138000",
            "password": NEW_PASSWORD,
            "next": destination,
        },
    )
    assert password_step.status_code == 302
    assert password_step["Location"] == "/login/verify/"
    signed_in = client.post(
        "/login/verify/", {"code": provider.last_code}
    )
    assert signed_in.status_code == 302
    assert signed_in["Location"] == destination


@pytest.mark.django_db
@pytest.mark.parametrize(
    "unsafe_destination",
    (
        "https://evil.example/records/",
        "//evil.example/records/",
        "/login/verify/",
        "/records/%252e%252e/admin/",
    ),
)
def test_unsafe_reset_destination_is_removed_from_link_and_rejected_by_state(
    client, active_account, provider, unsafe_destination
):
    login_page = client.get(
        f"/login/?{urlencode({'next': unsafe_destination})}"
    )
    assert login_page.status_code == 200
    assert 'href="/login/forgot-password/"' in login_page.content.decode()
    assert 'href="/login/forgot-password/?next=' not in login_page.content.decode()

    forgot_page = client.get(
        f"/login/forgot-password/?{urlencode({'next': unsafe_destination})}"
    )
    assert forgot_page.status_code == 200
    assert forgot_page.context["form"]["next"].value() == "/"
    requested = client.post(
        "/login/forgot-password/",
        {"phone": "13800138000", "next": unsafe_destination},
    )
    drain_sms_worker(provider)
    assert requested.status_code == 302
    pending = dict(client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY])
    assert pending["destination"] == "/"

    pending["destination"] = unsafe_destination
    session = client.session
    session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = pending
    session.save()
    verification = client.get("/login/forgot-password/verify/")
    assert verification.status_code == 200
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in client.session
    rejected = client.post(
        "/login/forgot-password/verify/", {"code": provider.last_code}
    )
    assert rejected.status_code == 400
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session


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

    phones = ("13800138000", "13900000000", "13700137000", "not-a-phone", "")
    clients = [Client() for _ in phones]
    posts = [
        reset_client.post("/login/forgot-password/", {"phone": phone})
        for reset_client, phone in zip(clients, phones)
    ]
    drain_sms_worker(provider)

    assert all(response.status_code == 302 for response in posts)
    assert all(
        response["Location"] == "/login/forgot-password/verify/" for response in posts
    )
    responses = [
        reset_client.get(response["Location"])
        for reset_client, response in zip(clients, posts)
    ]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.context["status"] == RESET_STATUS for response in responses)
    assert all(not response.context["form"].is_bound for response in responses)
    visible_responses = {
        re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b'name="csrfmiddlewaretoken"', response.content)
        for response in responses
    }
    assert len(visible_responses) == 1
    for reset_client, response, phone in zip(clients, responses, phones):
        if phone:
            assert phone not in response.content.decode()
        pending = reset_client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]
        assert set(pending) == {
            "account_id",
            "challenge_id",
            "destination",
            "issued_at",
        }
        assert pending["destination"] == "/"
        if phone:
            assert phone not in str(pending)
    assert OtpChallenge.objects.count() == 1
    assert OtpChallenge.objects.get().account == active_account

    decoy_failures = [
        reset_client.post(
            "/login/forgot-password/verify/", {"code": provider.last_code}
        )
        for reset_client in clients[1:]
    ]
    assert all(response.status_code == 400 for response in decoy_failures)
    assert len(
        {
            re.sub(
                rb'name="csrfmiddlewaretoken" value="[^"]+"',
                b'name="csrfmiddlewaretoken"',
                response.content,
            )
            for response in decoy_failures
        }
    ) == 1
    assert all(
        VERIFIED_PASSWORD_RESET_SESSION_KEY not in reset_client.session
        for reset_client in clients[1:]
    )


@pytest.mark.django_db
def test_reset_request_keeps_provider_failure_neutral_with_non_advanceable_decoy_state(
    client, active_account, monkeypatch
):
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: FailingSmsProvider())

    response = client.post("/login/forgot-password/", {"phone": "13800138000"})
    drain_sms_worker(FailingSmsProvider())

    assert response.status_code == 302
    assert response["Location"] == "/login/forgot-password/verify/"
    verification = client.get(response["Location"])
    assert verification.status_code == 200
    assert verification.context["status"] == RESET_STATUS
    assert "13800138000" not in verification.content.decode()
    assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY in client.session
    assert "13800138000" not in str(
        client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]
    )
    assert not OtpChallenge.objects.filter(
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
    ).exists()
    rejected = client.post(
        "/login/forgot-password/verify/", {"code": "123456"}
    )
    assert rejected.status_code == 400
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_decoy_reset_state_cannot_collide_with_a_real_account_challenge_pair(
    active_account, provider, monkeypatch
):
    real_client = Client()
    requested = real_client.post(
        "/login/forgot-password/", {"phone": "13800138000"}
    )
    drain_sms_worker(provider)
    assert requested.status_code == 302
    challenge = OtpChallenge.objects.get(
        purpose=OtpChallenge.Purpose.PASSWORD_RESET
    )

    monkeypatch.setattr("apps.accounts.views.uuid4", lambda: active_account.pk)
    monkeypatch.setattr(
        "apps.accounts.views.secrets.randbelow", lambda _upper: challenge.pk - 1
    )
    decoy_client = Client()
    decoy = decoy_client.post(
        "/login/forgot-password/", {"phone": "13900000000"}
    )
    drain_sms_worker(provider)
    assert decoy.status_code == 302

    rejected = decoy_client.post(
        "/login/forgot-password/verify/", {"code": provider.last_code}
    )

    assert rejected.status_code == 400
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in decoy_client.session
    challenge.refresh_from_db()
    assert challenge.consumed_at is None


@pytest.mark.django_db
def test_real_and_decoy_wrong_codes_keep_identical_copy_and_attempt_lifecycle(
    active_account, provider
):
    real_client = Client()
    decoy_client = Client()
    real = real_client.post(
        "/login/forgot-password/", {"phone": "13800138000"}
    )
    decoy = decoy_client.post(
        "/login/forgot-password/", {"phone": "13900000000"}
    )
    drain_sms_worker(provider)
    assert real["Location"] == decoy["Location"]
    wrong_code = "000000" if provider.last_code != "000000" else "000001"

    def scrub(body):
        return re.sub(
            rb'name="csrfmiddlewaretoken" value="[^"]+"',
            b'name="csrfmiddlewaretoken"',
            body,
        )

    for attempt in range(1, 6):
        real_failure = real_client.post(
            "/login/forgot-password/verify/", {"code": wrong_code}
        )
        decoy_failure = decoy_client.post(
            "/login/forgot-password/verify/", {"code": wrong_code}
        )

        assert real_failure.status_code == decoy_failure.status_code == 400
        assert scrub(real_failure.content) == scrub(decoy_failure.content)
        if attempt < 5:
            assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY in real_client.session
            assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY in decoy_client.session
        else:
            assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in real_client.session
            assert PASSWORD_RESET_PENDING_MFA_SESSION_KEY not in decoy_client.session

    real_missing = real_client.get("/login/forgot-password/verify/")
    decoy_missing = decoy_client.get("/login/forgot-password/verify/")
    assert real_missing.status_code == decoy_missing.status_code == 200
    assert scrub(real_missing.content) == scrub(decoy_missing.content)


@pytest.mark.django_db
def test_reset_forms_use_explicit_routes_require_csrf_and_work_without_javascript(client, active_account, provider):
    csrf_client = Client(enforce_csrf_checks=True)

    request_page = client.get("/login/forgot-password/")
    assert request_page.status_code == 200
    assert 'action="/login/forgot-password/"' in request_page.content.decode()
    assert csrf_client.post("/login/forgot-password/", {"phone": "13800138000"}).status_code == 403
    assert csrf_client.post("/login/forgot-password/verify/", {"code": "123456"}).status_code == 403
    assert csrf_client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    ).status_code == 403
    # Django's client never executes JavaScript: complete the real flow even
    # though optional browser link-state housekeeping is present in the shell.
    start_password_reset(client, provider)
    verify_password_reset(client, provider)
    completed = client.post("/login/forgot-password/new-password/", {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert completed.status_code == 302
    active_account.refresh_from_db()
    assert active_account.check_password(NEW_PASSWORD)


@pytest.mark.django_db
def test_reset_pending_and_verified_states_are_strict_separate_and_contain_no_secrets(
    client, active_account, provider
):
    start_password_reset(client, provider)
    pending = client.session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY]

    assert set(pending) == {
        "account_id",
        "challenge_id",
        "destination",
        "issued_at",
    }
    assert pending["account_id"] == str(active_account.pk)
    assert pending["destination"] == "/"
    assert "13800138000" not in str(pending)
    assert provider.last_code not in str(pending)
    assert VERIFIED_PASSWORD_RESET_SESSION_KEY not in client.session

    verify_password_reset(client, provider)
    verified = client.session[VERIFIED_PASSWORD_RESET_SESSION_KEY]
    assert set(verified) == {
        "account_id",
        "challenge_id",
        "destination",
        "verified_at",
    }
    assert verified["account_id"] == str(active_account.pk)
    assert verified["challenge_id"] == pending["challenge_id"]
    assert verified["destination"] == "/"
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
def test_reset_password_errors_have_focusable_summary_and_field_association(
    client,
    active_account,
    provider,
):
    start_password_reset(client, provider)
    verify_password_reset(client, provider)

    response = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": "Different passphrase 2026"},
    )
    content = response.content.decode()

    assert response.status_code == 400
    assert content.index('class="error-summary"') < content.index("<form")
    assert 'tabindex="-1"' in content
    assert 'href="#id_password2"' in content
    assert 'id="id_password_confirm_error"' in content
    assert 'aria-invalid="true"' in content
    assert 'aria-describedby="id_password_confirm_error"' in content
    assert content.count('id="id_password_confirm_error"') == 1


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
    call_command("register_legacy_sessions", verbosity=0)

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
    client, active_account, provider, monkeypatch
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
    challenge_count = OtpChallenge.objects.count()
    immediate_login = client.post(
        "/login/password/", {"phone": "13800138000", "password": NEW_PASSWORD}
    )
    assert immediate_login.status_code == 400
    assert OtpChallenge.objects.count() == challenge_count

    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
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
    challenge_id = pending["challenge_id"]
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
    terminal = OtpChallenge.objects.get(pk=challenge_id)
    assert terminal.consumed_at is not None
    assert terminal.locked_at is not None
    active_account.refresh_from_db()
    assert active_account.check_password(NEW_PASSWORD)


@pytest.mark.django_db
def test_sign_in_challenge_issued_before_reset_cannot_complete_afterward(
    client, active_account, provider, monkeypatch
):
    login = client.post(
        "/login/password/",
        {"phone": "13800138000", "password": "Old strong passphrase 2026"},
    )
    assert login.status_code == 302
    sign_in_challenge_id = client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]["challenge_id"]
    sign_in_code = provider.last_code

    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
    monkeypatch.setattr("apps.accounts.authentication.timezone.now", lambda: after_cooldown)
    reset_challenge = start_password_reset(client, provider)
    verify_password_reset(client, provider)
    completed = client.post(
        "/login/forgot-password/new-password/",
        {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )

    assert completed.status_code == 302
    with pytest.raises(LockedOtp):
        complete_password_login(sign_in_challenge_id, sign_in_code, active_account.pk)
    sign_in_challenge = OtpChallenge.objects.get(pk=sign_in_challenge_id)
    assert sign_in_challenge.consumed_at is None
    assert sign_in_challenge.locked_at is not None
    reset_challenge.refresh_from_db()
    assert reset_challenge.locked_at is not None


def _create_sent_challenge(*, phone_hash, ip_hash, created_at):
    challenge = OtpChallenge.objects.create(
        phone_hash=phone_hash,
        phone_encrypted="ciphertext",
        ip_hash=ip_hash,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="synthetic-hash",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=created_at + timedelta(minutes=5),
    )
    OtpChallenge.objects.filter(pk=challenge.pk).update(created_at=created_at)
    return challenge


@pytest.mark.django_db
def test_completed_reset_send_remains_in_phone_hour_and_day_accounting(
    client, active_account, provider, monkeypatch
):
    now = timezone.now()
    for index in range(4):
        _create_sent_challenge(
            phone_hash=active_account.phone_hash,
            ip_hash=hash_ip(f"203.0.113.{index + 10}"),
            created_at=now - timedelta(minutes=2),
        )

    response = complete_password_reset(client, provider)
    assert response.status_code == 302
    assert OtpChallenge.objects.filter(phone_hash=active_account.phone_hash).count() == 5

    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
    with pytest.raises(ThrottledOtp):
        request_otp(
            "13800138000",
            "198.51.100.1",
            provider,
            purpose=OtpChallenge.Purpose.SIGN_IN,
            account=active_account,
        )


@pytest.mark.django_db
def test_completed_reset_send_remains_in_ip_hour_accounting(
    client, active_account, provider, monkeypatch
):
    now = timezone.now()
    reset_ip = "127.0.0.1"
    for index in range(29):
        _create_sent_challenge(
            phone_hash=hash_phone(f"synthetic-phone-{index}"),
            ip_hash=hash_ip(reset_ip),
            created_at=now - timedelta(minutes=2),
        )

    response = complete_password_reset(client, provider)
    assert response.status_code == 302
    assert OtpChallenge.objects.filter(ip_hash=hash_ip(reset_ip)).count() == 30

    after_cooldown = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: after_cooldown)
    with pytest.raises(ThrottledOtp):
        request_otp(
            "13900139000",
            reset_ip,
            provider,
            purpose=OtpChallenge.Purpose.FIRST_USE,
        )


@pytest.mark.django_db
def test_login_page_shows_only_generic_reset_completion_status(client):
    response = client.get("/login/?password-reset=complete")

    assert response.status_code == 200
    assert response.context["status"]
    assert "13800138000" not in response.content.decode()
