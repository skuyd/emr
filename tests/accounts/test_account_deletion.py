from datetime import timedelta

from django.contrib.sessions.models import Session
from django.utils import timezone
import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion
from apps.accounts.models import Account, AccountDeletionJob, ConsentRecord, OtpChallenge, OtpThrottle
from apps.documents.deletion import purge_document_deletion
from apps.documents.models import Document, DocumentDeletionJob
from apps.patients.models import Patient, PatientPreference, ProductFeedback
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
        phone_hash=phone_hash,
        phone_encrypted="ciphertext",
        ip_hash="i" * 64,
        otp_hash="hash",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    OtpThrottle.objects.create(scope="phone", identifier_hash=phone_hash)
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
    assert not Session.objects.filter(session_key=session_key).exists()
