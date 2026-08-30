import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.documents.domain import InvalidTransition, transition_document
from apps.documents.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    ImmutableDocumentFieldError,
    PatientScopeError,
    ProcessingRun,
    UploadBatch,
    BatchStatus,
    InvalidDocumentMetadata,
    UploadItem,
    UploadItemStatus,
)


def _patient(django_user_model, marker):
    from apps.patients.models import Patient

    account = django_user_model.objects.create(phone_hash=marker * 64, phone_encrypted="ciphertext")
    return Patient.objects.create(account=account, display_name="test")


def _document(patient, sha256=None, batch=None, **overrides):
    batch = batch or UploadBatch.objects.create(patient=patient)
    values = {
        "patient": patient,
        "batch": batch,
        "display_filename": "report.pdf",
        "content_type": "application/pdf",
        "byte_size": 10,
        "page_count": 1,
        "sha256": sha256 or uuid.uuid4().hex * 2,
        "original_object_key": f"originals/{uuid.uuid4()}",
    }
    values.update(overrides)
    return Document.objects.create(**values)


@pytest.fixture
def patient(django_user_model):
    return _patient(django_user_model, "a")


@pytest.fixture
def document(patient):
    return _document(patient)


@pytest.mark.django_db
def test_saved_document_moves_from_processing_to_organized(document):
    transitioned = transition_document(document, DocumentStatus.ORGANIZED)

    assert transitioned.status == DocumentStatus.ORGANIZED


@pytest.mark.django_db
def test_upload_failed_is_not_a_persisted_document_state(document):
    with pytest.raises(InvalidTransition):
        transition_document(document, "UPLOAD_FAILED")


@pytest.mark.django_db
def test_processing_failure_keeps_original_reference_and_can_retry(document):
    original_key = document.original_object_key

    failed = transition_document(document, DocumentStatus.PROCESSING_FAILED)
    retried = transition_document(failed, DocumentStatus.PROCESSING)
    retried.refresh_from_db()

    assert retried.status == DocumentStatus.PROCESSING
    assert retried.original_object_key == original_key


@pytest.mark.django_db
def test_only_authoritative_document_transitions_and_idempotence_are_allowed(document):
    assert transition_document(document, DocumentStatus.PROCESSING).status == DocumentStatus.PROCESSING
    for target in (DocumentStatus.ORGANIZED, DocumentStatus.ORIGINAL_ONLY, DocumentStatus.PROCESSING_FAILED):
        fresh = _document(document.patient)
        assert transition_document(fresh, target).status == target
    finished = transition_document(document, DocumentStatus.ORGANIZED)
    with pytest.raises(InvalidTransition):
        transition_document(finished, DocumentStatus.PROCESSING_FAILED)


@pytest.mark.django_db
def test_document_has_only_durable_public_states_with_exact_chinese_labels():
    assert {choice.value: choice.label for choice in DocumentStatus} == {
        "PROCESSING": "处理中",
        "ORGANIZED": "已整理",
        "ORIGINAL_ONLY": "仅原件",
        "PROCESSING_FAILED": "处理失败",
    }


@pytest.mark.django_db
def test_document_requires_canonical_content_type_and_sha256(patient):
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, content_type="text/plain")
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, sha256="not-a-digest")
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, original_object_key="staging/temporary")
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, original_object_key="originals/../escape")
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, original_object_key="originals\\backslash")
    with pytest.raises(InvalidDocumentMetadata):
        _document(patient, original_object_key="originals/control\nkey")


@pytest.mark.django_db
def test_original_identity_metadata_cannot_change_after_insert(document):
    for field, replacement in (
        ("original_object_key", "originals/replaced"),
        ("sha256", "f" * 64),
        ("byte_size", 11),
        ("content_type", "image/png"),
        ("page_count", 2),
        ("display_filename", "renamed.pdf"),
    ):
        document.refresh_from_db()
        setattr(document, field, replacement)
        with pytest.raises(ImmutableDocumentFieldError):
            document.save()


@pytest.mark.django_db
def test_same_hash_is_patient_scoped_and_soft_deletion_allows_new_upload(django_user_model):
    first = _patient(django_user_model, "b")
    second = _patient(django_user_model, "c")
    digest = "d" * 64
    original = _document(first, digest)
    _document(second, digest)
    with pytest.raises(IntegrityError), transaction.atomic():
        _document(first, digest)

    original.deleted_at = timezone.now()
    original.save(update_fields=["deleted_at"])
    replacement = _document(first, digest)
    assert replacement.pk != original.pk


@pytest.mark.django_db
def test_upload_items_hold_transient_outcomes_and_batch_document_relationship(patient):
    batch = UploadBatch.objects.create(patient=patient, file_count=2, page_count=1, byte_size=10)
    created = _document(patient, batch=batch)
    failed = UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename="failed.pdf",
        status=UploadItemStatus.UPLOAD_FAILED,
        error_code="unreadable_file",
    )
    existing = UploadItem.objects.create(
        batch=batch,
        ordinal=2,
        display_filename="existing.pdf",
        status=UploadItemStatus.EXACT_DUPLICATE,
        document=created,
    )

    assert failed.document is None
    assert existing.document_id == created.id
    assert "UPLOAD_FAILED" not in set(DocumentStatus.values)
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(
            batch=batch,
            ordinal=3,
            display_filename="invalid.pdf",
            status=UploadItemStatus.UPLOAD_FAILED,
            document=created,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(
            batch=batch,
            ordinal=5,
            display_filename="reason.pdf",
            status=UploadItemStatus.UPLOAD_FAILED,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(
            batch=batch,
            ordinal=6,
            display_filename="unexpected-code.pdf",
            error_code="unreadable_file",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(
            batch=batch,
            ordinal=4,
            display_filename="missing.pdf",
            status=UploadItemStatus.CREATED,
        )


@pytest.mark.django_db
def test_created_item_must_link_its_own_batch_document_once_but_duplicate_may_link_old_batch(patient):
    old_batch = UploadBatch.objects.create(patient=patient)
    current_batch = UploadBatch.objects.create(patient=patient)
    old_document = _document(patient, batch=old_batch)
    current_document = _document(patient, batch=current_batch)
    with pytest.raises(PatientScopeError):
        UploadItem.objects.create(
            batch=current_batch,
            ordinal=1,
            display_filename="invalid.pdf",
            status=UploadItemStatus.CREATED,
            document=old_document,
        )
    UploadItem.objects.create(
        batch=current_batch,
        ordinal=2,
        display_filename="created.pdf",
        status=UploadItemStatus.CREATED,
        document=current_document,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(
            batch=current_batch,
            ordinal=3,
            display_filename="duplicate-created.pdf",
            status=UploadItemStatus.CREATED,
            document=current_document,
        )
    UploadItem.objects.create(
        batch=current_batch,
        ordinal=4,
        display_filename="duplicate.pdf",
        status=UploadItemStatus.EXACT_DUPLICATE,
        document=old_document,
    )


@pytest.mark.django_db
def test_batch_patient_scope_is_enforced_for_document_and_upload_item(django_user_model):
    first = _patient(django_user_model, "f")
    second = _patient(django_user_model, "g")
    first_batch = UploadBatch.objects.create(patient=first)
    second_document = _document(second)

    with pytest.raises(PatientScopeError):
        _document(second, batch=first_batch)
    with pytest.raises(PatientScopeError):
        UploadItem.objects.create(
            batch=first_batch,
            ordinal=1,
            display_filename="foreign.pdf",
            status=UploadItemStatus.EXACT_DUPLICATE,
            document=second_document,
        )


@pytest.mark.django_db
def test_batch_and_item_database_constraints(patient):
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadBatch.objects.create(patient=patient, file_count=21)
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadBatch.objects.create(patient=patient, page_count=61)
    batch = UploadBatch.objects.create(patient=patient)
    UploadItem.objects.create(batch=batch, ordinal=1, display_filename="one.pdf")
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadItem.objects.create(batch=batch, ordinal=1, display_filename="two.pdf")


@pytest.mark.django_db
def test_batch_has_completed_summary_state_for_future_task_card_retention(patient):
    batch = UploadBatch.objects.create(patient=patient)
    assert batch.status == BatchStatus.ACTIVE
    assert batch.completed_at is None
    batch.status = BatchStatus.COMPLETED
    with pytest.raises(IntegrityError), transaction.atomic():
        batch.save()
    batch.completed_at = timezone.now()
    batch.save(update_fields=["status", "completed_at"])
    assert batch.status == BatchStatus.COMPLETED


@pytest.mark.django_db
def test_page_and_processing_run_database_constraints(patient):
    document = _document(patient)
    DocumentPage.objects.create(document=document, page_number=1, width=100, height=200, orientation="PORTRAIT")
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentPage.objects.create(document=document, page_number=1, width=100, height=200, orientation="PORTRAIT")
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentPage.objects.create(document=document, page_number=0, width=100, height=200, orientation="PORTRAIT")

    run = ProcessingRun.objects.create(document=document, parser_version="v1", task_type="full", idempotency_key="run-1")
    assert run.is_current is False
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingRun.objects.create(document=document, parser_version="v2", task_type="full", idempotency_key="run-2")
    other = _document(document.patient)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingRun.objects.create(document=other, parser_version="v1", task_type="full", idempotency_key="run-1")

    run.stage = "SUCCEEDED"
    run.is_current = True
    with pytest.raises(IntegrityError), transaction.atomic():
        run.save(update_fields=["stage", "is_current"])
    run.refresh_from_db()
    run.stage = "SUCCEEDED"
    run.is_current = True
    run.finished_at = timezone.now()
    run.save(update_fields=["stage", "is_current", "finished_at"])
    ProcessingRun.objects.create(
        document=document,
        parser_version="v2",
        task_type="full",
        idempotency_key="run-3",
        attempt_number=2,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingRun.objects.create(
            document=document,
            parser_version="v3",
            task_type="full",
            idempotency_key="run-4",
            attempt_number=3,
            stage="SUCCEEDED",
            is_current=True,
            finished_at=timezone.now(),
        )
    assert any(index.fields == ["stage", "heartbeat_at"] for index in ProcessingRun._meta.indexes)


@pytest.mark.django_db
def test_purge_timestamp_requires_prior_soft_delete(document):
    document.purged_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        document.save()


@pytest.mark.django_db
def test_soft_deleted_document_rejects_every_status_transition_including_idempotence(document):
    document.deleted_at = timezone.now()
    document.save(update_fields=["deleted_at"])

    with pytest.raises(InvalidTransition):
        transition_document(document, DocumentStatus.PROCESSING)
    with pytest.raises(InvalidTransition):
        transition_document(document, DocumentStatus.ORGANIZED)


@pytest.mark.django_db
def test_patient_deletion_cascades_document_aggregate_despite_internal_references(patient):
    batch = UploadBatch.objects.create(patient=patient)
    document = _document(patient, batch=batch)
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename="created.pdf",
        status=UploadItemStatus.CREATED,
        document=document,
    )
    DocumentPage.objects.create(document=document, page_number=1)
    ProcessingRun.objects.create(document=document, parser_version="v1", task_type="full", idempotency_key="delete-run")
    patient.delete()

    assert not UploadBatch.objects.exists()
    assert not UploadItem.objects.exists()
    assert not Document.objects.exists()
    assert not DocumentPage.objects.exists()
    assert not ProcessingRun.objects.exists()
