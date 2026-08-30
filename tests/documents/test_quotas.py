import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.documents.models import Document, PatientUploadQuota
from apps.documents.quotas import (
    InvalidQuotaProposal,
    QuotaExceeded,
    QuotaLockRequired,
    QuotaProposal,
    check_upload_quota,
    lock_patient_quota,
)
from apps.documents.models import UploadBatch


GIB = 1024**3


def _patient(django_user_model):
    from apps.patients.models import Patient

    account = django_user_model.objects.create(phone_hash="q" * 64, phone_encrypted="ciphertext")
    return Patient.objects.create(account=account, display_name="test")


def _document(patient, *, byte_size=10, page_count=1):
    batch = UploadBatch.objects.create(patient=patient)
    return Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="quota.pdf",
        content_type="application/pdf",
        byte_size=byte_size,
        page_count=page_count,
        sha256=uuid.uuid4().hex * 2,
        original_object_key=f"originals/{uuid.uuid4()}",
    )


@pytest.mark.django_db
def test_default_quota_boundaries_accept_exact_limit_and_reject_one_beyond(django_user_model):
    patient = _patient(django_user_model)
    PatientUploadQuota.objects.create(patient=patient)
    exact = QuotaProposal(batch_files=20, batch_pages=60, documents=300, document_pages=1000, storage_bytes=2 * GIB)

    assert check_upload_quota(patient, exact).patient_id == patient.id
    for proposed, code in (
        (QuotaProposal(batch_files=21), "batch_file_limit"),
        (QuotaProposal(batch_pages=61), "batch_page_limit"),
        (QuotaProposal(documents=301), "document_limit"),
        (QuotaProposal(document_pages=1001), "page_limit"),
        (QuotaProposal(storage_bytes=2 * GIB + 1), "storage_limit"),
    ):
        with pytest.raises(QuotaExceeded) as raised:
            check_upload_quota(patient, proposed)
        assert raised.value.code == code


@pytest.mark.django_db
def test_adjustable_batch_limits_cannot_exceed_database_safety_caps(django_user_model):
    patient = _patient(django_user_model)
    with pytest.raises(IntegrityError), transaction.atomic():
        PatientUploadQuota.objects.create(patient=patient, batch_file_limit=21)
    with pytest.raises(IntegrityError), transaction.atomic():
        PatientUploadQuota.objects.create(patient=patient, batch_page_limit=61)


@pytest.mark.django_db
def test_existing_usage_and_proposal_apply_exact_boundaries(django_user_model):
    patient = _patient(django_user_model)
    quota = PatientUploadQuota.objects.create(
        patient=patient, document_limit=2, page_limit=2, storage_byte_limit=20
    )
    document = _document(patient, byte_size=10, page_count=1)
    assert check_upload_quota(patient, QuotaProposal(documents=1, document_pages=1, storage_bytes=10)).pk == quota.pk
    for proposal, code in (
        (QuotaProposal(documents=2), "document_limit"),
        (QuotaProposal(document_pages=2), "page_limit"),
        (QuotaProposal(storage_bytes=11), "storage_limit"),
    ):
        with pytest.raises(QuotaExceeded) as raised:
            check_upload_quota(patient, proposal)
        assert raised.value.code == code

    document.deleted_at = timezone.now()
    document.save(update_fields=["deleted_at"])

    with pytest.raises(QuotaExceeded) as raised:
        check_upload_quota(patient, QuotaProposal(documents=1, document_pages=1, storage_bytes=11))
    assert raised.value.code == "storage_limit"

    document.purged_at = timezone.now()
    document.save(update_fields=["purged_at"])
    assert check_upload_quota(patient, QuotaProposal(documents=1, document_pages=1, storage_bytes=11)).pk == quota.pk


@pytest.mark.django_db(transaction=True)
def test_quota_lock_helper_returns_one_to_one_row_for_future_fixed_lock_order(django_user_model):
    patient = _patient(django_user_model)

    with pytest.raises(QuotaLockRequired):
        lock_patient_quota(patient)
    with transaction.atomic():
        quota = lock_patient_quota(patient)

    assert quota.patient_id == patient.id
    assert PatientUploadQuota.objects.get(patient=patient).pk == quota.pk


@pytest.mark.django_db
def test_negative_quota_proposal_is_typed_rejection(django_user_model):
    patient = _patient(django_user_model)
    with pytest.raises(InvalidQuotaProposal):
        check_upload_quota(patient, QuotaProposal(storage_bytes=-1))


@pytest.mark.django_db
def test_quota_instance_cannot_be_reused_for_another_patient(django_user_model):
    first = _patient(django_user_model)
    second_account = django_user_model.objects.create(phone_hash="r" * 64, phone_encrypted="ciphertext")
    from apps.patients.models import Patient

    second = Patient.objects.create(account=second_account, display_name="test")
    first_quota = PatientUploadQuota.objects.create(patient=first)

    with pytest.raises(InvalidQuotaProposal):
        check_upload_quota(second, QuotaProposal(), quota=first_quota)


@pytest.mark.django_db
def test_supplied_quota_is_relocked_before_usage_is_recomputed(django_user_model, monkeypatch):
    patient = _patient(django_user_model)
    quota = PatientUploadQuota.objects.create(patient=patient)
    calls = []
    original = PatientUploadQuota.objects.select_for_update

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(PatientUploadQuota.objects, "select_for_update", tracked)
    check_upload_quota(patient, QuotaProposal(), quota=quota)

    assert calls
