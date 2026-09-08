from datetime import timedelta
import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_legacy_owners_tasks_preferences_and_permanent_deletions_keep_their_identities():
    executor = MigrationExecutor(connection)
    previous = {
        "patients": "0002_patientpreference_productfeedback",
        "documents": "0005_document_lifecycle_revision_document_purged_at_and_more",
        "facts": "0001_initial",
        "processing": "0004_ocr_layout_geometry",
        "exports": "0001_initial", "notifications": "0001_initial",
        "operations": "0006_supportaccessgrant_permission_revision",
        # These domains did not exist at the owner-only baseline. Keeping
        # current leaves would reapply family migrations mid-plan.
        "self_records": None, "glucose": None, "treatments": None, "lesions": None,
    }
    # Resolve existing main migrations by prefix; filenames remain authoritative.
    for app, prefix in tuple(previous.items()):
        if prefix is None:
            continue
        if (app, prefix) not in executor.loader.graph.nodes:
            previous[app] = next(name for candidate, name in executor.loader.graph.nodes
                                 if candidate == app and name.startswith(prefix[:4] + "_"))
    targets = [(app, previous.get(app, name)) for app, name in executor.loader.graph.leaf_nodes()]
    now = timezone.now()
    try:
        executor.migrate(targets)
        legacy = executor.loader.project_state([target for target in targets if target[1] is not None]).apps
        owner = legacy.get_model("accounts", "Account").objects.create(phone_hash="1" * 64, phone_encrypted="synthetic")
        inactive = legacy.get_model("accounts", "Account").objects.create(phone_hash="2" * 64, phone_encrypted="synthetic", is_active=False)
        legacy.get_model("accounts", "AccountDeletionJob").objects.create(account_id=inactive.pk)
        suspended = legacy.get_model("accounts", "Account").objects.create(phone_hash="3" * 64, phone_encrypted="synthetic", is_active=False)
        patient = legacy.get_model("patients", "Patient").objects.create(account_id=owner.pk, display_name="迁移患者")
        deleted_patient = legacy.get_model("patients", "Patient").objects.create(account_id=inactive.pk, display_name="已注销患者")
        suspended_patient = legacy.get_model("patients", "Patient").objects.create(account_id=suspended.pk, display_name="暂时停用账号的患者")
        legacy.get_model("patients", "PatientPreference").objects.create(patient_id=patient.pk, browser_notifications_enabled=True)
        batch = legacy.get_model("documents", "UploadBatch").objects.create(patient_id=patient.pk)
        document = legacy.get_model("documents", "Document").objects.create(
            patient_id=patient.pk, batch_id=batch.pk, display_filename="synthetic.png", content_type="image/png",
            byte_size=128, page_count=1, sha256="a" * 64, original_object_key=f"originals/{uuid.uuid4().hex}", deleted_at=now,
        )
        deletion = legacy.get_model("documents", "DocumentDeletionJob").objects.create(document_id=document.pk, object_key=document.original_object_key)
        run = legacy.get_model("documents", "ProcessingRun").objects.create(
            document_id=document.pk, parser_version="legacy", task_type="USER_RETRY_2", attempt_number=2,
            idempotency_key=f"{document.pk}:legacy:retry",
        )
        export = legacy.get_model("exports", "ExportJob").objects.create(
            patient_id=patient.pk, snapshot={"synthetic": True}, session_digest="e" * 64, expires_at=now + timedelta(hours=1),
        )
        subscription = legacy.get_model("notifications", "PushSubscription").objects.create(
            patient_id=patient.pk, endpoint_hash="f" * 64, endpoint_ciphertext="encrypted-synthetic",
            p256dh_ciphertext="encrypted-synthetic", auth_ciphertext="encrypted-synthetic",
        )
        notification = legacy.get_model("notifications", "TaskNotification").objects.create(
            patient_id=patient.pk, batch_id=batch.pk, kind="COMPLETED", read_at=now,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from apps.patients.models import Patient, PatientMembership, PatientPreference, PatientDeletionJob
        from apps.documents.models import Document, DocumentDeletionJob, ProcessingRun, UploadBatch
        from apps.exports.models import ExportJob
        from apps.notifications.models import NotificationReceipt, PushSubscription

        assert Patient.objects.get(pk=patient.pk).account_id == owner.pk
        assert PatientMembership.objects.get(patient_id=patient.pk, account_id=owner.pk).role == "ADMIN"
        assert PatientPreference.objects.get(patient_id=patient.pk, account_id=owner.pk).browser_notifications_enabled
        assert Document.objects.get(pk=document.pk).created_by_id == owner.pk
        assert UploadBatch.objects.get(pk=batch.pk).created_by_id == owner.pk
        assert ProcessingRun.objects.get(pk=run.pk).requested_by_id == owner.pk
        assert DocumentDeletionJob.objects.get(pk=deletion.pk).document_id == document.pk
        assert Document.objects.get(pk=document.pk).trashed_at is None
        migrated_export = ExportJob.objects.get(pk=export.pk)
        assert migrated_export.requested_by_id == owner.pk and migrated_export.session_digest == "e" * 64
        assert migrated_export.snapshot == {"synthetic": True}
        assert PushSubscription.objects.get(pk=subscription.pk).account_id == owner.pk
        assert NotificationReceipt.objects.get(notification_id=notification.pk, account_id=owner.pk).read_at == now
        assert Patient.objects.get(pk=deleted_patient.pk).deleted_at is not None
        assert PatientMembership.objects.get(patient_id=deleted_patient.pk).revoked_at is not None
        assert PatientDeletionJob.objects.filter(patient_id=deleted_patient.pk).exists()
        assert Patient.objects.get(pk=suspended_patient.pk).deleted_at is None
        assert PatientMembership.objects.get(patient_id=suspended_patient.pk).revoked_at is None
        assert not PatientDeletionJob.objects.filter(patient_id=suspended_patient.pk).exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
