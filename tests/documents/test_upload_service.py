from dataclasses import replace
import hashlib
import io
import logging
import uuid

from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone
from PIL import Image
import pytest

from apps.documents.errors import StorageTransportError
from apps.documents.inspection import inspect_upload
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentPage,
    PatientUploadQuota,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.documents.quotas import QuotaExceeded
from apps.documents.services import (
    ArtifactMismatch,
    InvalidProcessingIdentity,
    UploadOutcomeKind,
    UploadResourceNotFound,
    UploadStateConflict,
    finalize_upload,
)
from apps.patients.models import Patient
from tests.documents.fakes import InMemoryObjectStore


def png_bytes(size=(12, 8)):
    output = io.BytesIO()
    Image.new("RGB", size, "#718096").save(output, format="PNG")
    return output.getvalue()


def alternate_png_bytes(size=(12, 8)):
    output = io.BytesIO()
    Image.new("RGB", size, "#d97706").save(output, format="PNG")
    return output.getvalue()


def patient(django_user_model):
    marker = uuid.uuid4().hex * 2
    account = django_user_model.objects.create(phone_hash=marker, phone_encrypted="ciphertext")
    return Patient.objects.create(account=account, display_name="test")


def batch_item(owner, *, ordinal=1, name="private-report.png", batch=None, status=UploadItemStatus.PENDING):
    batch = batch or UploadBatch.objects.create(patient=owner)
    item = UploadItem.objects.create(
        batch=batch,
        ordinal=ordinal,
        display_filename=name,
        status=status,
        error_code="previous_failure" if status == UploadItemStatus.UPLOAD_FAILED else "",
    )
    return batch, item


def inspect_and_stage(store, payload=None, name="private-report.png"):
    payload = payload or png_bytes()
    artifact = inspect_upload(io.BytesIO(payload), name)
    with artifact.open() as source:
        staged = store.put_staging(
            source,
            expected_size=artifact.byte_size,
            expected_sha256=artifact.sha256,
        )
    return artifact, staged


def stored_document(owner, store, payload=None, *, deleted_at=None, purged_at=None):
    payload = payload or png_bytes()
    artifact, staged = inspect_and_stage(store, payload)
    batch = UploadBatch.objects.create(patient=owner)
    immutable = store.promote_immutable(staged, f"originals/{uuid.uuid4().hex}")
    document = Document.objects.create(
        patient=owner,
        batch=batch,
        display_filename="existing.png",
        content_type=artifact.content_type,
        byte_size=artifact.byte_size,
        page_count=artifact.page_count,
        sha256=artifact.sha256,
        original_object_key=immutable.key,
        deleted_at=deleted_at,
        purged_at=purged_at,
    )
    artifact.close()
    return document


@pytest.mark.django_db(transaction=True)
def test_success_promotes_before_record_and_atomically_creates_pages_run_item_and_batch(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    dispatches = []
    original_create = Document.objects.create

    def assert_durable_before_create(**values):
        assert values["original_object_key"] in store.objects
        return original_create(**values)

    def dispatch_after_commit(run_id):
        assert Document.objects.filter(pk=item.pk).exists()
        assert ProcessingRun.objects.filter(pk=run_id).exists()
        dispatches.append(run_id)

    monkeypatch.setattr(Document.objects, "create", assert_durable_before_create)
    try:
        outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store, dispatch_after_commit)
    finally:
        artifact.close()

    document = Document.objects.get(pk=outcome.document_id)
    item.refresh_from_db()
    batch.refresh_from_db()
    run = ProcessingRun.objects.get(pk=outcome.processing_run_id)
    page = DocumentPage.objects.get(document=document)
    assert outcome.kind == UploadOutcomeKind.CREATED
    assert outcome.saved is True
    assert document.status == "PROCESSING"
    assert document.original_object_key in store.objects
    assert (document.byte_size, document.page_count, document.sha256) == (
        len(png_bytes()),
        1,
        hashlib.sha256(png_bytes()).hexdigest(),
    )
    assert (page.page_number, page.width, page.height, page.orientation) == (1, 12, 8, "LANDSCAPE")
    assert (item.status, item.document_id, item.error_code) == (UploadItemStatus.CREATED, document.pk, "")
    assert (batch.file_count, batch.page_count, batch.byte_size, batch.status) == (
        1,
        1,
        len(png_bytes()),
        BatchStatus.ACTIVE,
    )
    assert (run.stage, run.is_current) == (ProcessingStage.QUEUED, False)
    assert run.idempotency_key == f"{document.pk}:phr-v1:INITIAL_PARSE"
    assert dispatches == [str(run.pk)]


@pytest.mark.django_db(transaction=True)
def test_broker_failure_after_commit_keeps_saved_document_and_durable_queued_run(django_user_model, caplog):
    owner = patient(django_user_model)
    batch, item = batch_item(owner, name="must-not-appear.png")
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)

    def unavailable(_run_id):
        raise RuntimeError("must-not-appear.png broker detail")

    with caplog.at_level(logging.WARNING, logger="apps.documents.services"):
        try:
            outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store, unavailable)
        finally:
            artifact.close()

    assert Document.objects.filter(pk=outcome.document_id).exists()
    assert ProcessingRun.objects.filter(pk=outcome.processing_run_id, stage=ProcessingStage.QUEUED).exists()
    assert "processing_dispatch_failed" in caplog.text
    assert "must-not-appear" not in caplog.text
    assert "broker detail" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_promotion_failure_creates_no_archive_result(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    store.fail_promote = True

    try:
        with pytest.raises(StorageTransportError):
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()

    item.refresh_from_db()
    assert item.status == UploadItemStatus.PENDING
    assert not Document.objects.exists()
    assert not ProcessingRun.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_retryable_failed_item_can_succeed_and_clears_safe_error_code(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner, status=UploadItemStatus.UPLOAD_FAILED)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    try:
        outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()
    item.refresh_from_db()
    assert outcome.kind == UploadOutcomeKind.CREATED
    assert item.status == UploadItemStatus.CREATED
    assert item.error_code == ""


@pytest.mark.django_db(transaction=True)
def test_database_failure_after_promotion_rolls_back_rows_and_compensates_exact_object(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)

    def fail_run(**_values):
        raise DatabaseError("private-report.png database detail")

    monkeypatch.setattr(ProcessingRun.objects, "create", fail_run)
    try:
        with pytest.raises(DatabaseError):
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()

    item.refresh_from_db()
    assert item.status == UploadItemStatus.PENDING
    assert not Document.objects.exists()
    assert not DocumentPage.objects.exists()
    assert not ProcessingRun.objects.exists()
    assert not any(key.startswith("originals/") for key in store.objects)
    assert any(call[0] == "delete" and call[1] == f"originals/{item.pk.hex}" for call in store.calls)


@pytest.mark.django_db(transaction=True)
def test_outer_database_commit_failure_compensates_promoted_original(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    original_commit = connection.commit
    attempts = 0
    dispatches = []

    def fail_first_commit():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DatabaseError("commit unavailable")
        return original_commit()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(connection, "commit", fail_first_commit)
            with pytest.raises(DatabaseError):
                finalize_upload(owner, batch.pk, item.pk, artifact, staged, store, dispatches.append)
    finally:
        artifact.close()

    assert attempts == 1
    assert not Document.objects.exists()
    assert f"originals/{item.pk.hex}" not in store.objects
    assert dispatches == []


@pytest.mark.django_db(transaction=True)
def test_database_failure_never_deletes_preexisting_same_byte_immutable(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, first_stage = inspect_and_stage(store)
    preexisting = store.promote_immutable(first_stage, f"originals/{item.pk.hex}")
    with artifact.open() as source:
        retry_stage = store.put_staging(source, expected_size=artifact.byte_size, expected_sha256=artifact.sha256)
    monkeypatch.setattr(ProcessingRun.objects, "create", lambda **_values: (_ for _ in ()).throw(DatabaseError("fail")))

    try:
        with pytest.raises(DatabaseError):
            finalize_upload(owner, batch.pk, item.pk, artifact, retry_stage, store)
    finally:
        artifact.close()

    assert not Document.objects.exists()
    with store.open_private(preexisting) as source:
        assert source.read() == png_bytes()


@pytest.mark.django_db(transaction=True)
def test_same_patient_exact_duplicate_creates_no_object_document_or_run(django_user_model):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    payload = png_bytes()
    existing = stored_document(owner, store, payload)
    PatientUploadQuota.objects.create(
        patient=owner,
        document_limit=1,
        page_limit=1,
        storage_byte_limit=len(payload),
    )
    batch, item = batch_item(owner)
    artifact, staged = inspect_and_stage(store, payload)
    store.calls.clear()

    try:
        outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()

    item.refresh_from_db()
    assert outcome.kind == UploadOutcomeKind.EXACT_DUPLICATE
    assert outcome.document_id == existing.pk
    assert item.status == UploadItemStatus.EXACT_DUPLICATE
    assert item.document_id == existing.pk
    assert Document.objects.filter(patient=owner).count() == 1
    assert not ProcessingRun.objects.exists()
    assert staged.key not in store.objects
    assert not any(call[0] == "promote_immutable" for call in store.calls)


@pytest.mark.django_db(transaction=True)
def test_duplicate_staging_cleanup_failure_cannot_reverse_committed_outcome(django_user_model, caplog):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    payload = png_bytes()
    existing = stored_document(owner, store, payload)
    batch, item = batch_item(owner)
    artifact, staged = inspect_and_stage(store, payload)
    store.fail_delete = True

    with caplog.at_level(logging.WARNING, logger="apps.documents.services"):
        try:
            outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
        finally:
            artifact.close()

    assert outcome.document_id == existing.pk
    assert UploadItem.objects.get(pk=item.pk).status == UploadItemStatus.EXACT_DUPLICATE
    assert staged.key in store.objects
    assert "staging_cleanup_deferred" in caplog.text
    assert "private-report" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_cross_patient_same_bytes_create_independent_private_documents(django_user_model):
    first = patient(django_user_model)
    second = patient(django_user_model)
    store = InMemoryObjectStore()
    outcomes = []
    artifacts = []
    try:
        for owner in (first, second):
            batch, item = batch_item(owner)
            artifact, staged = inspect_and_stage(store)
            artifacts.append(artifact)
            outcomes.append(finalize_upload(owner, batch.pk, item.pk, artifact, staged, store))
    finally:
        for artifact in artifacts:
            artifact.close()

    documents = list(Document.objects.order_by("created_at"))
    assert [outcome.kind for outcome in outcomes] == [UploadOutcomeKind.CREATED, UploadOutcomeKind.CREATED]
    assert len(documents) == 2
    assert documents[0].sha256 == documents[1].sha256
    assert documents[0].patient_id != documents[1].patient_id
    assert documents[0].original_object_key != documents[1].original_object_key


@pytest.mark.django_db(transaction=True)
def test_soft_deleted_hash_can_be_reuploaded_with_new_key_but_unpurged_bytes_still_count(django_user_model):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    payload = png_bytes()
    old = stored_document(owner, store, payload, deleted_at=timezone.now())
    quota = PatientUploadQuota.objects.create(patient=owner, storage_byte_limit=len(payload))
    batch, item = batch_item(owner)
    artifact, staged = inspect_and_stage(store, payload)

    try:
        with pytest.raises(QuotaExceeded) as raised:
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
        assert raised.value.code == "storage_limit"

        old.purged_at = timezone.now()
        old.save(update_fields=["purged_at"])
        store.delete(old.original_object_key)
        quota.storage_byte_limit = len(payload)
        quota.save(update_fields=["storage_byte_limit"])
        outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()

    replacement = Document.objects.get(pk=outcome.document_id)
    assert replacement.original_object_key != old.original_object_key
    assert Document.objects.filter(patient=owner, deleted_at__isnull=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_account_document_quota_is_rechecked_under_finalization_lock(django_user_model):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    stored_document(owner, store, png_bytes())
    PatientUploadQuota.objects.create(patient=owner, document_limit=1)
    batch, item = batch_item(owner)
    artifact, staged = inspect_and_stage(store, alternate_png_bytes())
    try:
        with pytest.raises(QuotaExceeded) as raised:
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()
    assert raised.value.code == "document_limit"
    assert Document.objects.filter(patient=owner).count() == 1
    assert f"originals/{item.pk.hex}" not in store.objects


@pytest.mark.django_db(transaction=True)
def test_batch_file_and_page_limits_are_recomputed_from_durable_items(django_user_model):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    batch, target = batch_item(owner)
    for ordinal in range(2, 22):
        UploadItem.objects.create(batch=batch, ordinal=ordinal, display_filename=f"reserved-{ordinal}.png")
    artifact, staged = inspect_and_stage(store)
    try:
        with pytest.raises(QuotaExceeded) as raised:
            finalize_upload(owner, batch.pk, target.pk, artifact, staged, store)
        assert raised.value.code == "batch_file_limit"
    finally:
        artifact.close()

    page_batch, page_target = batch_item(owner)
    UploadItem.objects.create(batch=page_batch, ordinal=2, display_filename="sixty.pdf", page_count=60)
    artifact, staged = inspect_and_stage(store)
    try:
        with pytest.raises(QuotaExceeded) as raised:
            finalize_upload(owner, page_batch.pk, page_target.pk, artifact, staged, store)
        assert raised.value.code == "batch_page_limit"
    finally:
        artifact.close()
    assert not Document.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_batch_exact_page_boundary_is_accepted(django_user_model):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    batch, target = batch_item(owner)
    UploadItem.objects.create(batch=batch, ordinal=2, display_filename="fifty-nine.pdf", page_count=59)
    artifact, staged = inspect_and_stage(store)
    try:
        outcome = finalize_upload(owner, batch.pk, target.pk, artifact, staged, store)
    finally:
        artifact.close()
    batch.refresh_from_db()
    assert outcome.kind == UploadOutcomeKind.CREATED
    assert batch.page_count == 60


@pytest.mark.django_db(transaction=True)
def test_batch_and_item_are_resolved_through_patient_scope(django_user_model):
    first = patient(django_user_model)
    second = patient(django_user_model)
    foreign_batch, foreign_item = batch_item(first)
    own_batch, _ = batch_item(second)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    try:
        with pytest.raises(UploadResourceNotFound):
            finalize_upload(second, foreign_batch.pk, foreign_item.pk, artifact, staged, store)
        with pytest.raises(UploadResourceNotFound):
            finalize_upload(second, own_batch.pk, foreign_item.pk, artifact, staged, store)
    finally:
        artifact.close()
    assert not Document.objects.exists()
    assert not PatientUploadQuota.objects.filter(patient=second).exists()
    assert not any(call[0] == "promote_immutable" for call in store.calls)


@pytest.mark.django_db(transaction=True)
def test_terminal_item_is_idempotent_and_cleans_redundant_stage(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    first = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    with artifact.open() as source:
        redundant = store.put_staging(source, expected_size=artifact.byte_size, expected_sha256=artifact.sha256)
    second = finalize_upload(owner, batch.pk, item.pk, artifact, redundant, store)
    artifact.close()

    assert second == first
    assert Document.objects.count() == 1
    assert ProcessingRun.objects.count() == 1
    assert redundant.key not in store.objects


@pytest.mark.django_db(transaction=True)
def test_terminal_idempotency_uses_exact_custom_parser_and_task_identity(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    first = finalize_upload(
        owner,
        batch.pk,
        item.pk,
        artifact,
        staged,
        store,
        parser_version="parser-2",
        task_type="CUSTOM_PARSE",
    )
    ProcessingRun.objects.create(
        document_id=first.document_id,
        parser_version="historical",
        task_type="CUSTOM_PARSE",
        idempotency_key=f"{first.document_id}:historical:CUSTOM_PARSE",
        stage=ProcessingStage.FAILED,
        finished_at=timezone.now(),
    )
    with artifact.open() as source:
        redundant = store.put_staging(source, expected_size=artifact.byte_size, expected_sha256=artifact.sha256)

    repeated = finalize_upload(
        owner,
        batch.pk,
        item.pk,
        artifact,
        redundant,
        store,
        parser_version="parser-2",
        task_type="CUSTOM_PARSE",
    )
    assert repeated == first

    with artifact.open() as source:
        wrong_version_stage = store.put_staging(
            source,
            expected_size=artifact.byte_size,
            expected_sha256=artifact.sha256,
        )
    with pytest.raises(UploadStateConflict):
        finalize_upload(
            owner,
            batch.pk,
            item.pk,
            artifact,
            wrong_version_stage,
            store,
            parser_version="parser-3",
            task_type="CUSTOM_PARSE",
        )
    artifact.close()


@pytest.mark.django_db(transaction=True)
def test_invalid_processing_identity_is_rejected_before_database_or_storage(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    try:
        with pytest.raises(InvalidProcessingIdentity):
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store, parser_version="bad:version")
    finally:
        artifact.close()
    assert not PatientUploadQuota.objects.filter(patient=owner).exists()
    assert not Document.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_terminal_item_cannot_be_replayed_with_different_verified_bytes(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    first_artifact, first_stage = inspect_and_stage(store)
    finalize_upload(owner, batch.pk, item.pk, first_artifact, first_stage, store)
    first_artifact.close()
    different_artifact, different_stage = inspect_and_stage(store, alternate_png_bytes())

    try:
        with pytest.raises(UploadStateConflict):
            finalize_upload(owner, batch.pk, item.pk, different_artifact, different_stage, store)
    finally:
        different_artifact.close()

    assert Document.objects.count() == 1
    assert different_stage.key in store.objects


@pytest.mark.django_db(transaction=True)
def test_closed_or_mismatched_artifact_is_rejected_before_database_or_storage(django_user_model):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    mismatched = replace(staged, sha256="0" * 64)

    with pytest.raises(ArtifactMismatch):
        finalize_upload(owner, batch.pk, item.pk, artifact, mismatched, store)
    artifact.close()
    with pytest.raises(ArtifactMismatch):
        finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    assert not Document.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_completed_batch_rejects_new_finalization(django_user_model):
    owner = patient(django_user_model)
    batch = UploadBatch.objects.create(patient=owner, status=BatchStatus.COMPLETED, completed_at=timezone.now())
    _, item = batch_item(owner, batch=batch)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    try:
        with pytest.raises(UploadStateConflict):
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()
    assert not Document.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_partial_unique_race_returns_same_patient_winner_and_compensates_loser(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    store = InMemoryObjectStore()
    payload = png_bytes()
    winner = stored_document(owner, store, payload)
    batch, item = batch_item(owner)
    artifact, staged = inspect_and_stage(store, payload)
    answers = iter([None, winner])
    monkeypatch.setattr("apps.documents.services.find_exact_duplicate", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(Document.objects, "create", lambda **_values: (_ for _ in ()).throw(IntegrityError("race")))

    try:
        outcome = finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()

    assert outcome.kind == UploadOutcomeKind.EXACT_DUPLICATE
    assert outcome.document_id == winner.pk
    assert f"originals/{item.pk.hex}" not in store.objects
    with store.open_private(winner.original_object_key) as source:
        assert source.read() == payload


@pytest.mark.django_db(transaction=True)
def test_unrelated_integrity_error_is_not_misreported_as_duplicate(django_user_model, monkeypatch):
    owner = patient(django_user_model)
    batch, item = batch_item(owner)
    store = InMemoryObjectStore()
    artifact, staged = inspect_and_stage(store)
    monkeypatch.setattr("apps.documents.services.find_exact_duplicate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Document.objects, "create", lambda **_values: (_ for _ in ()).throw(IntegrityError("other")))

    try:
        with pytest.raises(IntegrityError):
            finalize_upload(owner, batch.pk, item.pk, artifact, staged, store)
    finally:
        artifact.close()
    assert f"originals/{item.pk.hex}" not in store.objects


def test_sqlite_tests_do_not_claim_postgresql_row_lock_equivalence():
    assert connection.vendor in {"sqlite", "postgresql"}
    if connection.vendor == "sqlite":
        assert connection.features.has_select_for_update is False
