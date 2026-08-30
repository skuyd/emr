import uuid

from django.utils import timezone
import pytest

from apps.documents.models import Document, UploadBatch
from apps.documents.similarity import find_possible_duplicate, perceptual_hash_distance
from apps.patients.models import Patient


def patient(django_user_model):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    return Patient.objects.create(account=account, display_name="测试患者")


def document(owner, perceptual_hash, *, deleted_at=None):
    marker = uuid.uuid4().hex
    batch = UploadBatch.objects.create(patient=owner)
    return Document.objects.create(
        patient=owner,
        batch=batch,
        display_filename="synthetic.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=marker * 2,
        perceptual_hash=perceptual_hash,
        original_object_key=f"originals/{marker}",
        deleted_at=deleted_at,
    )


def test_perceptual_hash_distance_accepts_only_lowercase_64_bit_hex_and_has_exact_boundary():
    assert perceptual_hash_distance("0000000000000000", "00000000000000ff") == 8
    assert perceptual_hash_distance("0000000000000000", "00000000000001ff") == 9
    for malformed in ("", "0" * 15, "0" * 17, "00000000000000FF", "g" * 16, None):
        assert perceptual_hash_distance("0000000000000000", malformed) is None


@pytest.mark.django_db
def test_possible_duplicate_distance_eight_hints_nine_does_not_and_query_never_mutates(django_user_model):
    owner = patient(django_user_model)
    near = document(owner, "00000000000000ff")
    far = document(owner, "00000000000001ff")
    before = list(Document.objects.values_list("pk", "perceptual_hash"))

    hint = find_possible_duplicate(owner, "0000000000000000")

    assert hint == near.pk
    assert hint != far.pk
    assert list(Document.objects.values_list("pk", "perceptual_hash")) == before


@pytest.mark.django_db
def test_possible_duplicate_never_crosses_patient_or_returns_deleted_or_current_document(django_user_model):
    owner = patient(django_user_model)
    foreign = patient(django_user_model)
    current = document(owner, "0000000000000000")
    deleted = document(owner, "0000000000000001", deleted_at=timezone.now())
    foreign_document = document(foreign, "0000000000000000")

    hint = find_possible_duplicate(
        owner,
        "0000000000000000",
        exclude_document_id=current.pk,
    )

    assert hint is None
    assert deleted.pk != hint
    assert foreign_document.pk != hint


@pytest.mark.django_db
def test_malformed_perceptual_hash_returns_no_hint(django_user_model):
    owner = patient(django_user_model)
    document(owner, "0000000000000000")

    assert find_possible_duplicate(owner, "000000000000000") is None
    assert find_possible_duplicate(owner, "000000000000000A") is None
