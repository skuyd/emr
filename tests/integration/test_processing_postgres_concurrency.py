from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

from django.db import close_old_connections, connection
import pytest

from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentStatus,
    ProcessingRun,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.patients.models import Patient
from apps.processing.runner import ExecutionState, PipelineResult, run_processing


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Run with --ds=config.settings.postgres_test and PHR_POSTGRES_TEST_URL")


def _patient(django_user_model):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    return Patient.objects.create(account=account, display_name="并发验收")


def _document_and_run(patient, batch, ordinal):
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename=f"synthetic-{ordinal}.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=ordinal,
        display_filename=f"synthetic-{ordinal}.png",
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=document,
    )
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="postgres-v1",
        task_type="concurrency",
        idempotency_key=f"{document.pk}:postgres-v1:concurrency",
    )
    return document, run


def _thread_run(run_id, pipeline):
    close_old_connections()
    try:
        return run_processing(run_id, pipeline)
    finally:
        close_old_connections()


def test_postgresql_duplicate_delivery_runs_the_pipeline_once(django_user_model):
    patient = _patient(django_user_model)
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document, run = _document_and_run(patient, batch, 1)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def pipeline(_context):
        with calls_lock:
            calls.append(True)
        entered.set()
        assert release.wait(timeout=10)
        return PipelineResult.organized()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_thread_run, run.pk, pipeline)
        assert entered.wait(timeout=10)
        duplicate = executor.submit(_thread_run, run.pk, pipeline)
        duplicate_result = duplicate.result(timeout=10)
        release.set()
        first_result = first.result(timeout=10)

    assert {first_result.state, duplicate_result.state} == {
        ExecutionState.SUCCEEDED,
        ExecutionState.ALREADY_RUNNING,
    }
    assert calls == [True]
    document.refresh_from_db()
    assert document.status == DocumentStatus.ORGANIZED


def test_postgresql_same_batch_terminal_updates_serialize_without_deadlock(django_user_model):
    patient = _patient(django_user_model)
    batch = UploadBatch.objects.create(patient=patient, file_count=2, page_count=2, byte_size=256)
    first_document, first_run = _document_and_run(patient, batch, 1)
    second_document, second_run = _document_and_run(patient, batch, 2)
    ready = threading.Barrier(2, timeout=10)

    def pipeline(_context):
        ready.wait()
        return PipelineResult.organized()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_thread_run, first_run.pk, pipeline),
            executor.submit(_thread_run, second_run.pk, pipeline),
        ]
        results = [future.result(timeout=15) for future in futures]

    assert [result.state for result in results] == [ExecutionState.SUCCEEDED, ExecutionState.SUCCEEDED]
    first_document.refresh_from_db()
    second_document.refresh_from_db()
    batch.refresh_from_db()
    assert first_document.status == DocumentStatus.ORGANIZED
    assert second_document.status == DocumentStatus.ORGANIZED
    assert batch.status == BatchStatus.COMPLETED
