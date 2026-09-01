from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.authentication import (
    EnrollmentUnavailable,
    ExistingAccountRequiresLogin,
    create_or_upgrade_account,
)
from apps.accounts.crypto import decrypt_phone, encrypt_phone, hash_phone
from apps.accounts.flow_state import (
    ENROLLMENT_PENDING_MFA_SESSION_KEY,
    PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
    SIGN_IN_PENDING_MFA_SESSION_KEY,
)
from apps.accounts.models import Account, ConsentRecord, OtpChallenge
from apps.analytics.models import ProductEvent
from apps.documents.models import Document, UploadBatch
from apps.patients.services import create_patient_space
from tests.accounts.fakes import FailingSmsProvider, RecordingSmsProvider


PHONE = "13800138000"
CANONICAL_PHONE = "+8613800138000"
PASSWORD = "Strong passphrase 2026"
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "first-use-test"}
VERIFIED_PHONE_SESSION_KEY = "verified_phone"


@pytest.fixture
def provider(monkeypatch):
    value = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: value)
    return value


@pytest.fixture
def legacy_account(db):
    return Account.objects.create_user(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
    )


@pytest.fixture
def legacy_records(legacy_account):
    patient = create_patient_space(
        legacy_account,
        "\u738b\u5c0f\u660e",
        CONFIRMATIONS,
        EVIDENCE,
    )
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="legacy.pdf",
        content_type="application/pdf",
        byte_size=128,
        page_count=1,
        sha256="a" * 64,
        original_object_key="originals/legacy-document",
    )
    return patient, document, tuple(
        ConsentRecord.objects.filter(account=legacy_account)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def start_first_use(client, provider, *, phone=PHONE, destination="/"):
    response = client.post("/login/first-use/", {"phone": phone, "next": destination})
    assert response.status_code == 302
    assert response["Location"] == "/login/first-use/verify/"
    return provider.last_code


def verify_first_use(client, code):
    response = client.post("/login/first-use/verify/", {"code": code})
    assert response.status_code == 302
    assert response["Location"] == "/login/first-use/password/"


def complete_first_use_flow(client, provider, *, phone=PHONE, destination="/", password=PASSWORD):
    code = start_first_use(client, provider, phone=phone, destination=destination)
    verify_first_use(client, code)
    return client.post(
        "/login/first-use/password/",
        {"password": password, "password_confirm": password},
    )


@pytest.mark.django_db
def test_requesting_and_verifying_first_use_code_never_creates_account(client, provider):
    code = start_first_use(client, provider, destination="/records/?tab=1")

    assert Account.objects.count() == 0
    challenge = OtpChallenge.objects.get()
    assert challenge.purpose == OtpChallenge.Purpose.FIRST_USE
    assert challenge.account_id is None
    assert provider.last_purpose == OtpChallenge.Purpose.FIRST_USE
    assert set(client.session[ENROLLMENT_PENDING_MFA_SESSION_KEY]) == {
        "challenge_id",
        "destination",
        "issued_at",
    }
    assert client.session[ENROLLMENT_PENDING_MFA_SESSION_KEY]["destination"] == "/records/?tab=1"
    assert PHONE not in str(client.session[ENROLLMENT_PENDING_MFA_SESSION_KEY])

    verify_first_use(client, code)

    challenge.refresh_from_db()
    assert challenge.consumed_at is not None
    assert Account.objects.count() == 0
    assert set(client.session[VERIFIED_PHONE_SESSION_KEY]) == {"challenge_id", "verified_at"}
    assert client.session[VERIFIED_PHONE_SESSION_KEY]["challenge_id"] == challenge.pk
    assert client.session[ENROLLMENT_PENDING_MFA_SESSION_KEY]["destination"] == "/records/?tab=1"


@pytest.mark.django_db
def test_first_use_state_is_saved_only_after_provider_accepts(client, monkeypatch):
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: FailingSmsProvider())

    response = client.post("/login/first-use/", {"phone": PHONE, "next": "/records/"})

    assert response.status_code == 400
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PHONE_SESSION_KEY not in client.session
    assert Account.objects.count() == 0


@pytest.mark.django_db
def test_verify_and_password_steps_ignore_posted_identity_and_destination(client, provider):
    code = start_first_use(client, provider, destination="/records/?tab=1")

    response = client.post(
        "/login/first-use/verify/",
        {"code": code, "phone": "13900139000", "challenge_id": "999", "next": "//evil.example"},
    )
    assert response["Location"] == "/login/first-use/password/"
    challenge = OtpChallenge.objects.get()
    challenge.phone_encrypted = encrypt_phone("+8613900139000")
    challenge.phone_hash = hash_phone("+8613900139000")
    challenge.save(update_fields=["phone_hash", "phone_encrypted"])

    response = client.post(
        "/login/first-use/password/",
        {
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "phone": PHONE,
            "challenge_id": "1",
            "next": "//evil.example",
        },
    )

    account = Account.objects.get()
    assert response["Location"] == "/onboarding/"
    assert account.phone_hash == hash_phone("+8613900139000")
    assert decrypt_phone(account.phone_encrypted) == "+8613900139000"
    assert client.session["post_onboarding_next"] == "/records/?tab=1"


@pytest.mark.django_db
def test_password_step_rejects_mismatched_or_stale_verified_state_and_clears_it(client, provider):
    code = start_first_use(client, provider)
    verify_first_use(client, code)
    session = client.session
    session[VERIFIED_PHONE_SESSION_KEY] = {
        "challenge_id": session[VERIFIED_PHONE_SESSION_KEY]["challenge_id"] + 1,
        "verified_at": session[VERIFIED_PHONE_SESSION_KEY]["verified_at"],
    }
    session.save()

    response = client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD},
    )

    assert response.status_code == 400
    assert Account.objects.count() == 0
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PHONE_SESSION_KEY not in client.session

    other_client = Client()
    code = start_first_use(other_client, provider, phone="13900139000")
    verify_first_use(other_client, code)
    session = other_client.session
    session[VERIFIED_PHONE_SESSION_KEY]["verified_at"] -= 300
    session.save()
    response = other_client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD},
    )
    assert response.status_code == 400
    assert Account.objects.count() == 0
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in other_client.session
    assert VERIFIED_PHONE_SESSION_KEY not in other_client.session


@pytest.mark.django_db
@override_settings(
    AUTH_PASSWORD_VALIDATORS=[
        {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 30}}
    ]
)
def test_password_requires_exact_confirmation_and_django_validation(client, provider):
    code = start_first_use(client, provider)
    verify_first_use(client, code)

    mismatch = client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD + "!"},
    )
    weak = client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD},
    )

    assert mismatch.status_code == 400
    assert weak.status_code == 400
    assert Account.objects.count() == 0
    assert PASSWORD not in mismatch.content.decode()
    assert PASSWORD not in weak.content.decode()
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY in client.session
    assert VERIFIED_PHONE_SESSION_KEY in client.session


@pytest.mark.django_db
def test_verified_new_account_is_created_only_at_password_step_and_sent_to_onboarding(client, provider):
    session = client.session
    session[SIGN_IN_PENDING_MFA_SESSION_KEY] = {"stale": True}
    session[PASSWORD_RESET_PENDING_MFA_SESSION_KEY] = {"stale": True}
    session.save()
    before_session_key = client.session.session_key

    response = complete_first_use_flow(client, provider, destination="/records/?tab=1")

    account = Account.objects.get()
    assert response.status_code == 302
    assert response["Location"] == "/onboarding/"
    assert account.check_password(PASSWORD)
    assert decrypt_phone(account.phone_encrypted) == CANONICAL_PHONE
    assert client.session.session_key != before_session_key
    assert client.session["_auth_user_id"] == str(account.pk)
    assert client.session["post_onboarding_next"] == "/records/?tab=1"
    assert isinstance(client.session["session_started_at"], int)
    assert ProductEvent.objects.filter(name="login_succeeded").count() == 1
    for key in (
        SIGN_IN_PENDING_MFA_SESSION_KEY,
        ENROLLMENT_PENDING_MFA_SESSION_KEY,
        PASSWORD_RESET_PENDING_MFA_SESSION_KEY,
        VERIFIED_PHONE_SESSION_KEY,
    ):
        assert key not in client.session


@pytest.mark.django_db
def test_verified_legacy_account_is_upgraded_in_place_with_all_ownership_preserved(
    client, provider, legacy_account, legacy_records
):
    patient, document, consent_ids = legacy_records
    original_account_id = legacy_account.pk
    original_patient_id = patient.pk
    original_document_id = document.pk

    response = complete_first_use_flow(client, provider, destination="/records/")

    legacy_account.refresh_from_db()
    patient.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 302
    assert response["Location"] == "/records/"
    assert legacy_account.pk == original_account_id
    assert legacy_account.check_password(PASSWORD)
    assert patient.pk == original_patient_id
    assert patient.account_id == original_account_id
    assert document.pk == original_document_id
    assert document.patient_id == original_patient_id
    assert tuple(
        ConsentRecord.objects.filter(account=legacy_account)
        .order_by("pk")
        .values_list("pk", flat=True)
    ) == consent_ids
    assert Account.objects.count() == 1
    assert client.session["_auth_user_id"] == str(legacy_account.pk)


@pytest.mark.django_db
def test_successful_existing_patient_login_clears_stale_onboarding_destination(
    client, provider, legacy_account, legacy_records
):
    session = client.session
    session["post_onboarding_next"] = "/stale-safe-destination/"
    session.save()

    response = complete_first_use_flow(client, provider, destination="/records/")

    assert response["Location"] == "/records/"
    assert client.session["_auth_user_id"] == str(legacy_account.pk)
    assert "post_onboarding_next" not in client.session


@pytest.mark.django_db
def test_usable_account_is_never_overwritten_or_logged_in_by_first_use(client, provider):
    account = Account.objects.create_user(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        password="Existing password 2026",
    )

    response = complete_first_use_flow(client, provider, password="Replacement password 2026")

    account.refresh_from_db()
    assert response.status_code == 400
    assert account.check_password("Existing password 2026")
    assert not account.check_password("Replacement password 2026")
    assert "login" in response.content.decode().lower()
    assert "reset" in response.content.decode().lower()
    assert "_auth_user_id" not in client.session
    assert Account.objects.count() == 1


@pytest.mark.django_db
def test_disabled_legacy_account_is_not_reactivated_or_duplicated(client, provider, legacy_account):
    legacy_account.is_active = False
    legacy_account.save(update_fields=["is_active"])

    response = complete_first_use_flow(client, provider)

    legacy_account.refresh_from_db()
    assert response.status_code == 400
    assert not legacy_account.is_active
    assert not legacy_account.has_usable_password()
    assert "_auth_user_id" not in client.session
    assert Account.objects.count() == 1


@pytest.mark.django_db
def test_first_use_posts_require_csrf_and_verify_requires_pending_state(client):
    csrf_client = Client(enforce_csrf_checks=True)

    assert csrf_client.post("/login/first-use/", {"phone": PHONE}).status_code == 403
    assert csrf_client.post("/login/first-use/verify/", {"code": "123456"}).status_code == 403
    assert csrf_client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD},
    ).status_code == 403
    assert client.get("/login/first-use/verify/").status_code == 400
    assert client.get("/login/first-use/password/").status_code == 400


@pytest.mark.django_db
def test_expired_challenge_cannot_reach_password_step(client, provider):
    code = start_first_use(client, provider)
    challenge = OtpChallenge.objects.get()
    challenge.expires_at = timezone.now() - timedelta(seconds=1)
    challenge.save(update_fields=["expires_at"])

    response = client.post("/login/first-use/verify/", {"code": code})

    assert response.status_code == 400
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PHONE_SESSION_KEY not in client.session
    assert Account.objects.count() == 0


@pytest.mark.django_db
def test_replayed_verification_invalidates_enrollment_and_password_cannot_advance(client, provider):
    code = start_first_use(client, provider)
    verify_first_use(client, code)

    replay = client.post("/login/first-use/verify/", {"code": code})

    assert replay.status_code == 400
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PHONE_SESSION_KEY not in client.session
    password = client.post(
        "/login/first-use/password/",
        {"password": PASSWORD, "password_confirm": PASSWORD},
    )
    assert password.status_code == 400
    assert Account.objects.count() == 0


@pytest.mark.django_db
def test_malformed_verification_clears_all_enrollment_state(client, provider):
    start_first_use(client, provider)

    response = client.post("/login/first-use/verify/", {"code": "not-six-digits"})

    assert response.status_code == 400
    assert ENROLLMENT_PENDING_MFA_SESSION_KEY not in client.session
    assert VERIFIED_PHONE_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_account_transition_rejects_a_consumed_challenge_older_than_five_minutes():
    challenge = OtpChallenge.objects.create(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        ip_hash="f" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() - timedelta(seconds=1),
        consumed_at=timezone.now() - timedelta(seconds=300),
    )

    with pytest.raises(EnrollmentUnavailable):
        create_or_upgrade_account(challenge, PASSWORD)

    assert Account.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("password", [None, ""])
def test_account_transition_rejects_missing_password_values(password):
    challenge = OtpChallenge.objects.create(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        ip_hash="e" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )

    with pytest.raises(ValidationError):
        create_or_upgrade_account(challenge, password)

    assert Account.objects.count() == 0


@pytest.mark.django_db
def test_unique_conflict_loser_gets_login_guidance_without_overwrite(monkeypatch):
    existing = Account.objects.create_user(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        password="Existing password 2026",
    )
    challenge = OtpChallenge.objects.create(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        ip_hash="d" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    original_first = QuerySet.first
    account_lookups = 0

    def stale_first(queryset):
        nonlocal account_lookups
        if queryset.model is Account:
            account_lookups += 1
            if account_lookups == 1:
                return None
        return original_first(queryset)

    monkeypatch.setattr(QuerySet, "first", stale_first)

    with pytest.raises(ExistingAccountRequiresLogin):
        create_or_upgrade_account(challenge, "Replacement password 2026")

    existing.refresh_from_db()
    assert existing.check_password("Existing password 2026")
    assert not existing.check_password("Replacement password 2026")
    assert Account.objects.count() == 1


@pytest.mark.django_db
def test_policy_conflict_at_password_step_does_not_create_or_upgrade_account(client, provider, settings):
    code = start_first_use(client, provider)
    verify_first_use(client, code)
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "a" * 64

    with override_settings(CONSENT_POLICIES=policies):
        response = client.post(
            "/login/first-use/password/",
            {"password": PASSWORD, "password_confirm": PASSWORD},
        )

    assert response.status_code == 503
    assert Account.objects.count() == 0
    assert "_auth_user_id" not in client.session
