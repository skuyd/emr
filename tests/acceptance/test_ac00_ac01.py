import re
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.flow_state import SIGN_IN_PENDING_MFA_SESSION_KEY
from apps.accounts.models import Account, AccountSession, ConsentRecord, OtpChallenge
from apps.accounts.phone import normalize_mainland_phone
from apps.accounts.session import IDLE_TIMEOUT_SECONDS
from apps.patients.models import Patient
from apps.patients.services import create_patient_space
from tests.accounts.fakes import RecordingSmsProvider


FIRST_USE_PASSWORD = "Strong acceptance passphrase 2026"
RESET_PASSWORD = "Replacement acceptance passphrase 2026"


class _InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


def _create_password_account(phone, password):
    normalized = normalize_mainland_phone(phone)
    return Account.objects.create_user(
        phone_hash=hash_phone(normalized),
        phone_encrypted=encrypt_phone(normalized),
        password=password,
    )


def _first_use(client, provider, phone, password, destination="/"):
    requested = client.post("/login/first-use/", {"phone": phone, "next": destination})
    assert requested.status_code == 302
    assert requested["Location"] == "/login/first-use/verify/"
    assert provider.last_purpose == OtpChallenge.Purpose.FIRST_USE

    verified = client.post("/login/first-use/verify/", {"code": provider.last_code})
    assert verified.status_code == 302
    assert verified["Location"] == "/login/first-use/password/"

    completed = client.post(
        "/login/first-use/password/",
        {"password": password, "password_confirm": password},
    )
    assert completed.status_code == 302
    return completed


def _password_mfa_login(client, provider, phone, password, destination="/"):
    password_step = client.post(
        "/login/password/",
        {"phone": phone, "password": password, "next": destination},
    )
    assert password_step.status_code == 302
    assert password_step["Location"] == "/login/verify/"
    assert provider.last_purpose == OtpChallenge.Purpose.SIGN_IN
    pending = client.session[SIGN_IN_PENDING_MFA_SESSION_KEY]
    assert set(pending) == {"account_id", "challenge_id", "destination", "issued_at"}
    assert phone not in str(pending)
    assert password not in str(pending)
    assert provider.last_code not in str(pending)
    assert "_auth_user_id" not in client.session

    verified = client.post("/login/verify/", {"code": provider.last_code})
    assert verified.status_code == 302
    return verified


def _assert_anonymous(client):
    assert "_auth_user_id" not in client.session
    assert "session_started_at" not in client.session


def _assert_secret_free(client, response, caplog, *secrets):
    evidence = "\n".join(
        (
            str(dict(client.session)),
            response.content.decode(errors="replace"),
            caplog.text,
        )
    )
    for secret in secrets:
        assert secret not in evidence


@pytest.mark.django_db
def test_ac00_ac01_first_use_password_mfa_state_safe_return_and_one_patient(
    client, monkeypatch, caplog
):
    """AC-00/AC-01 use only public HTTP auth flows and synthetic credentials."""
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    phone = "13900000000"
    safe_next = "/records/?source=acceptance"

    assert Account.objects.count() == 0
    first_use_response = _first_use(client, provider, phone, FIRST_USE_PASSWORD, safe_next)
    assert first_use_response["Location"] == "/onboarding/"
    assert Account.objects.count() == 1
    account = Account.objects.get()
    assert account.check_password(FIRST_USE_PASSWORD)
    assert client.session["_auth_user_id"] == str(account.pk)
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session

    onboarding = client.get("/onboarding/")
    assert onboarding.status_code == 200
    parser = _InputParser()
    parser.feed(onboarding.content.decode())
    business_inputs = [input_ for input_ in parser.inputs if input_.get("name") != "csrfmiddlewaretoken"]
    expected_fields = {"display_name", "privacy", "sensitive_data", "upload_authority"}
    assert {input_["name"] for input_ in business_inputs} == expected_fields
    assert {input_["name"] for input_ in business_inputs if "required" in input_} == expected_fields

    completed = client.post(
        "/onboarding/",
        {
            "display_name": "测试称呼",
            "privacy": "on",
            "sensitive_data": "on",
            "upload_authority": "on",
        },
    )
    assert completed.status_code == 302
    assert completed["Location"] == safe_next

    patient = Patient.objects.get(account=account)
    assert set(
        ConsentRecord.objects.filter(account=account, withdrawn_at__isnull=True).values_list(
            "consent_type", flat=True
        )
    ) == {"privacy", "sensitive_data", "upload_authority"}
    assert create_patient_space(
        account,
        "different synthetic label",
        {},
        {"ip": "127.0.0.1", "user_agent": "acceptance-test"},
    ).pk == patient.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        Patient.objects.create(account=account, display_name="another synthetic label")

    session = client.session
    now = timezone.now()
    session["session_started_at"] = int(now.timestamp())
    session["session_last_seen_at"] = int((now - timedelta(seconds=IDLE_TIMEOUT_SECONDS)).timestamp())
    session.save()
    expired_session = client.get(safe_next)
    assert expired_session.status_code == 302
    assert expired_session["Location"] == "/login/?next=%2Frecords%2F%3Fsource%3Dacceptance"
    _assert_anonymous(client)

    otp_count = OtpChallenge.objects.count()
    wrong_password = client.post(
        "/login/password/",
        {"phone": phone, "password": "Wrong acceptance passphrase", "next": safe_next},
    )
    assert wrong_password.status_code == 400
    assert OtpChallenge.objects.count() == otp_count
    assert len(provider.codes) == 1
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in client.session
    _assert_anonymous(client)
    _assert_secret_free(client, wrong_password, caplog, phone, "Wrong acceptance passphrase", provider.last_code)

    later = now + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: later)
    logged_in = _password_mfa_login(client, provider, phone, FIRST_USE_PASSWORD, safe_next)
    assert logged_in["Location"] == safe_next
    assert client.session["_auth_user_id"] == str(account.pk)
    assert Account.objects.count() == 1
    assert Patient.objects.get(account=account).pk == patient.pk

    missing_client = Client()
    missing = missing_client.post("/login/verify/", {"code": provider.last_code})
    assert missing.status_code == 400
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in missing_client.session
    _assert_anonymous(missing_client)
    _assert_secret_free(missing_client, missing, caplog, phone, FIRST_USE_PASSWORD, provider.last_code)

    expired_phone = "13700000000"
    expired_password = "Expired pending passphrase 2026"
    expired_account = _create_password_account(expired_phone, expired_password)
    create_patient_space(
        expired_account,
        "过期状态验收",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "acceptance-test"},
    )
    expired_client = Client()
    password_step = expired_client.post(
        "/login/password/",
        {"phone": expired_phone, "password": expired_password, "next": "https://evil.example/"},
    )
    assert password_step.status_code == 302
    expired_code = provider.last_code
    expired_pending = dict(expired_client.session[SIGN_IN_PENDING_MFA_SESSION_KEY])
    assert expired_pending["destination"] == "/"
    expired_pending["issued_at"] -= 300
    expired_session_data = expired_client.session
    expired_session_data[SIGN_IN_PENDING_MFA_SESSION_KEY] = expired_pending
    expired_session_data.save()
    rejected = expired_client.post("/login/verify/", {"code": expired_code})
    assert rejected.status_code == 400
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in expired_client.session
    _assert_anonymous(expired_client)
    _assert_secret_free(expired_client, rejected, caplog, expired_phone, expired_password, expired_code)


@pytest.mark.django_db
def test_ac00_first_use_upgrades_legacy_account_in_place_and_preserves_ownership(client, monkeypatch):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    phone = "13600000000"
    normalized = normalize_mainland_phone(phone)
    legacy = Account.objects.create(
        phone_hash=hash_phone(normalized),
        phone_encrypted=encrypt_phone(normalized),
    )
    legacy.set_unusable_password()
    legacy.save(update_fields=["password"])
    patient = create_patient_space(
        legacy,
        "既有档案",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "acceptance-test"},
    )
    consent_ids = set(ConsentRecord.objects.filter(account=legacy).values_list("pk", flat=True))

    completed = _first_use(client, provider, phone, FIRST_USE_PASSWORD, "/records/?source=legacy")

    assert completed["Location"] == "/records/?source=legacy"
    assert Account.objects.count() == 1
    upgraded = Account.objects.get()
    assert upgraded.pk == legacy.pk
    assert upgraded.check_password(FIRST_USE_PASSWORD)
    assert Patient.objects.get(pk=patient.pk).account_id == legacy.pk
    assert set(ConsentRecord.objects.filter(account=legacy).values_list("pk", flat=True)) == consent_ids
    assert client.session["_auth_user_id"] == str(legacy.pk)


@pytest.mark.django_db
def test_ac00_reset_is_neutral_revokes_current_and_old_sessions_and_requires_fresh_mfa(
    monkeypatch, caplog
):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    phone = "13800000000"
    old_password = "Old acceptance passphrase 2026"
    account = _create_password_account(phone, old_password)
    create_patient_space(
        account,
        "重置验收",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "acceptance-test"},
    )

    current = Client()
    current.force_login(account)
    old = Client()
    old.force_login(account)
    old_keys = {current.session.session_key, old.session.session_key}
    assert AccountSession.objects.filter(account=account).count() == 2

    existing = current.post("/login/forgot-password/", {"phone": phone})
    missing_client = Client()
    missing = missing_client.post("/login/forgot-password/", {"phone": "13500000000"})
    assert existing.status_code == missing.status_code == 200
    assert existing.context["status"] == missing.context["status"]
    scrub = lambda body: re.sub(
        rb'name="csrfmiddlewaretoken" value="[^"]+"', rb'name="csrfmiddlewaretoken"', body
    )
    assert scrub(existing.content) == scrub(missing.content)
    assert phone not in existing.content.decode()
    assert "13500000000" not in missing.content.decode()
    assert OtpChallenge.objects.filter(purpose=OtpChallenge.Purpose.PASSWORD_RESET).count() == 1

    reset_code = provider.last_code
    verified = current.post("/login/forgot-password/verify/", {"code": reset_code})
    assert verified.status_code == 302
    assert verified["Location"] == "/login/forgot-password/new-password/"
    reset = current.post(
        "/login/forgot-password/new-password/",
        {"password": RESET_PASSWORD, "password_confirm": RESET_PASSWORD},
    )
    assert reset.status_code == 302
    assert reset["Location"] == "/login/?password-reset=complete"
    account.refresh_from_db()
    assert account.check_password(RESET_PASSWORD)
    assert not Session.objects.filter(session_key__in=old_keys).exists()
    assert not AccountSession.objects.filter(account=account).exists()
    _assert_anonymous(current)
    assert old.get("/").status_code == 302

    challenge_count = OtpChallenge.objects.count()
    rejected_old_password = current.post(
        "/login/password/", {"phone": phone, "password": old_password}
    )
    assert rejected_old_password.status_code == 400
    assert OtpChallenge.objects.count() == challenge_count
    assert SIGN_IN_PENDING_MFA_SESSION_KEY not in current.session
    _assert_anonymous(current)
    _assert_secret_free(current, rejected_old_password, caplog, phone, old_password, reset_code)

    later = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.accounts.services._now", lambda: later)
    fresh = _password_mfa_login(current, provider, phone, RESET_PASSWORD)
    assert fresh["Location"] == "/"
    assert current.session["_auth_user_id"] == str(account.pk)
    assert Account.objects.count() == 1
