from datetime import timedelta

from django.contrib.sessions.models import Session
from django.utils import timezone
import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion
from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.models import (
    Account,
    AccountDeletionJob,
    ConsentRecord,
    OtpChallenge,
    OtpThrottle,
    PasswordAttemptThrottle,
)
from apps.accounts.services import LockedOtp, request_otp
from apps.documents.deletion import purge_document_deletion
from apps.documents.models import Document, DocumentDeletionJob
from apps.patients.models import Patient, PatientPreference, ProductFeedback
from apps.notifications.models import PushSubscription
from apps.notifications.services import upsert_push_subscription
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_account_delete_confirmation_immediately_disables_access_and_queues_every_document(
    django_user_model, django_capture_on_commit_callbacks, monkeypatch
):
    client, patient = _patient(django_user_model, "4")
    account = patient.account
    second_session = type(client)()
    second_session.force_login(account)
    documents = [_document(patient, content_type="image/png", page_count=1)[0] for _ in range(2)]
    document_dispatches = []
    account_dispatches = []
    current_session_key = client.session.session_key
    second_session_key = second_session.session.session_key
    upsert_push_subscription(
        patient,
        "https://push.example.test/subscriptions/delete-me",
        "BNcW8V8wLwVhZk5wYlN5dGhldGljS2V5VGhhdElzTG9uZ0Vub3VnaA",
        "c3ludGhldGljLWF1dGg",
    )
    monkeypatch.setattr(
        "apps.patients.views.safe_enqueue_document_deletion",
        lambda job_id: document_dispatches.append(job_id),
    )
    monkeypatch.setattr(
        "apps.patients.views.safe_enqueue_account_deletion",
        lambda job_id: account_dispatches.append(job_id),
    )
    path = "/me/delete-account/"

    confirmation = client.get(path)
    rejected = client.post(path, {})

    assert confirmation.status_code == 200
    assert "请再次确认" in confirmation.content.decode()
    assert "不可恢复" in confirmation.content.decode()
    assert rejected.status_code == 400
    account.refresh_from_db()
    assert account.is_active is True

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(path, {"confirmation": "delete-account"})

    assert response.status_code == 302 and response["Location"] == "/account-deleted/"
    account.refresh_from_db()
    assert account.is_active is False
    assert not account.has_usable_password()
    assert all(Document.objects.get(pk=item.pk).deleted_at is not None for item in documents)
    document_jobs = tuple(DocumentDeletionJob.objects.filter(document__in=documents).values_list("pk", flat=True))
    account_job = AccountDeletionJob.objects.get(account=account)
    assert set(document_dispatches) == set(document_jobs)
    assert account_dispatches == [account_job.pk]
    assert "账号访问已停止" in client.get(response["Location"]).content.decode()
    assert client.get("/").status_code == 302
    assert second_session.get("/").status_code == 302
    assert not Session.objects.filter(session_key__in=[current_session_key, second_session_key]).exists()
    assert not PushSubscription.objects.filter(patient=patient).exists()


def test_account_purge_waits_for_originals_then_removes_credentials_consents_preferences_and_sessions(
    django_user_model, django_capture_on_commit_callbacks, monkeypatch
):
    client, patient = _patient(django_user_model, "5")
    account = patient.account
    account_id = account.pk
    phone_hash = account.phone_hash
    second_session = type(client)()
    second_session.force_login(account)
    document = _document(patient, content_type="image/png", page_count=1)[0]
    PatientPreference.objects.create(patient=patient, browser_notifications_enabled=True)
    ProductFeedback.objects.create(patient=patient, message="synthetic product feedback")
    OtpChallenge.objects.create(
        purpose=OtpChallenge.Purpose.FIRST_USE,
        phone_hash=phone_hash,
        phone_encrypted="ciphertext",
        ip_hash="i" * 64,
        otp_hash="hash",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    OtpThrottle.objects.create(scope="phone", identifier_hash=phone_hash)
    PasswordAttemptThrottle.objects.create(
        scope="phone",
        identifier_hash=phone_hash,
        window_started_at=timezone.now(),
        attempts=1,
    )
    session_key = second_session.session.session_key
    monkeypatch.setattr("apps.patients.views.safe_enqueue_document_deletion", lambda _job_id: None)
    monkeypatch.setattr("apps.patients.views.safe_enqueue_account_deletion", lambda _job_id: None)
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/me/delete-account/", {"confirmation": "delete-account"})
    account_job = AccountDeletionJob.objects.get(account_id=account_id)
    document_job = DocumentDeletionJob.objects.get(document=document)

    waiting = purge_account_deletion(account_job.pk)

    assert waiting.outcome == AccountDeletionOutcome.RETRY_SCHEDULED
    account_job.refresh_from_db()
    assert account_job.error_code == "document_deletion_pending"
    assert Account.objects.filter(pk=account_id).exists()

    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic-private-object"
    purge_document_deletion(document_job.pk, store)
    finished = purge_account_deletion(account_job.pk)

    assert finished.outcome == AccountDeletionOutcome.PURGED
    assert not Account.objects.filter(pk=account_id).exists()
    assert not Patient.objects.filter(pk=patient.pk).exists()
    assert not ConsentRecord.objects.filter(account_id=account_id).exists()
    assert not PatientPreference.objects.filter(patient_id=patient.pk).exists()
    assert not ProductFeedback.objects.filter(patient_id=patient.pk).exists()
    assert not OtpChallenge.objects.filter(phone_hash=phone_hash).exists()
    assert not OtpThrottle.objects.filter(scope="phone", identifier_hash=phone_hash).exists()
    assert not PasswordAttemptThrottle.objects.filter(scope="phone", identifier_hash=phone_hash).exists()
    assert not Session.objects.filter(session_key=session_key).exists()


def test_otp_cannot_be_requested_for_an_account_with_deletion_in_progress(django_user_model):
    phone = "+8613800138000"
    phone_digest = hash_phone(phone)
    account = django_user_model.objects.create(
        phone_hash=phone_digest,
        phone_encrypted=encrypt_phone(phone),
        is_active=False,
    )
    Patient.objects.create(account=account, display_name="测试用户")
    with pytest.raises(LockedOtp):
        request_otp(
            phone,
            "203.0.113.9",
            purpose=OtpChallenge.Purpose.SIGN_IN,
            account=account,
        )

    account.refresh_from_db()
    assert account.is_active is False
