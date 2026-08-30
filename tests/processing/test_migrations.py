import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest

from apps.documents.models import Document, ProcessingRun, ProcessingStage, UploadBatch
from apps.patients.models import Patient


@pytest.mark.django_db(transaction=True)
def test_0002_requeues_legacy_running_rows_before_enforcing_lease_constraint(django_user_model):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    patient = Patient.objects.create(account=account, display_name="迁移验收")
    batch = UploadBatch.objects.create(patient=patient)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="synthetic.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="legacy-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:legacy-v1:initial",
    )
    old_heartbeat = timezone.now()
    ProcessingRun.objects.filter(pk=run.pk).update(
        stage=ProcessingStage.OCR,
        heartbeat_at=old_heartbeat,
        lease_token=uuid.uuid4(),
    )

    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("documents", "0001_initial")])
        legacy_apps = executor.loader.project_state([("documents", "0001_initial")]).apps
        legacy_run = legacy_apps.get_model("documents", "ProcessingRun").objects.get(pk=run.pk)
        assert legacy_run.stage == ProcessingStage.OCR

        executor = MigrationExecutor(connection)
        executor.migrate([("documents", "0002_processingrun_lease_token")])
        current_apps = executor.loader.project_state(
            [("documents", "0002_processingrun_lease_token")]
        ).apps
        migrated = current_apps.get_model("documents", "ProcessingRun").objects.get(pk=run.pk)
        assert migrated.stage == ProcessingStage.QUEUED
        assert migrated.lease_token is None
        assert migrated.next_retry_at is not None
        assert migrated.next_retry_at == migrated.heartbeat_at
        assert migrated.error_code == "processing_migration_requeued"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
