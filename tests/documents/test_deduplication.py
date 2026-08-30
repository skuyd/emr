import uuid

import pytest
from django.utils import timezone

from apps.documents.deduplication import InvalidDuplicateDigest, find_exact_duplicate
from apps.documents.models import Document, UploadBatch
from apps.patients.models import Patient


def patient(django_user_model):
    marker = uuid.uuid4().hex * 2
    account = django_user_model.objects.create(phone_hash=marker, phone_encrypted="ciphertext")
    return Patient.objects.create(account=account, display_name="test")


def document(owner, digest):
    batch = UploadBatch.objects.create(patient=owner)
    return Document.objects.create(
        patient=owner,
        batch=batch,
        display_filename="safe.pdf",
        content_type="application/pdf",
        byte_size=10,
        page_count=1,
        sha256=digest,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )


@pytest.mark.django_db
def test_exact_duplicate_query_is_scoped_to_one_patient(django_user_model):
    first = patient(django_user_model)
    second = patient(django_user_model)
    digest = "a" * 64
    first_document = document(first, digest)
    document(second, digest)

    assert find_exact_duplicate(first, digest).pk == first_document.pk


@pytest.mark.django_db
def test_another_patients_digest_is_indistinguishable_from_no_match(django_user_model):
    first = patient(django_user_model)
    second = patient(django_user_model)
    digest = "b" * 64
    document(first, digest)

    assert find_exact_duplicate(second, digest) is None


@pytest.mark.django_db
def test_soft_deleted_document_is_not_an_active_exact_duplicate(django_user_model):
    owner = patient(django_user_model)
    existing = document(owner, "c" * 64)
    existing.deleted_at = timezone.now()
    existing.save(update_fields=["deleted_at"])

    assert find_exact_duplicate(owner, existing.sha256) is None


@pytest.mark.django_db
def test_invalid_digest_is_rejected_before_query(django_user_model):
    owner = patient(django_user_model)
    with pytest.raises(InvalidDuplicateDigest) as raised:
        find_exact_duplicate(owner, "not-a-hash")
    assert str(raised.value) == "invalid_duplicate_digest"
