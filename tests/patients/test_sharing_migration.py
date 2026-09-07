from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_task_five_migration_preserves_existing_members_exports_and_append_only_audit():
    executor = MigrationExecutor(connection)
    previous = {"patients": "0004_migrate_family_ownership", "operations": "0007_alter_deletiontombstone_kind"}
    targets = [(app, previous.get(app, name)) for app, name in executor.loader.graph.leaf_nodes()]
    try:
        executor.migrate(targets)
        old = executor.loader.project_state(targets).apps
        account = old.get_model("accounts", "Account").objects.create(phone_hash="d" * 64, phone_encrypted="synthetic")
        patient = old.get_model("patients", "Patient").objects.create(account_id=account.pk, display_name="迁移合成患者")
        member = old.get_model("patients", "PatientMembership").objects.create(patient_id=patient.pk, account_id=account.pk, role="ADMIN", revision=7)
        job = old.get_model("exports", "ExportJob").objects.create(patient_id=patient.pk, requested_by_id=account.pk, access_revision=7,
            session_digest="a" * 64, snapshot={"synthetic": "preserved"}, expires_at=timezone.now() + timedelta(hours=1))
        audit = old.get_model("operations", "AuditEvent").objects.create(actor_hash="b" * 64, action="patient_created", target_hash="c" * 64, result="succeeded")
        expected = {"id": audit.pk, "actor_hash": audit.actor_hash, "target_hash": audit.target_hash, "action": audit.action,
                    "result": audit.result, "created_at": audit.created_at}
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from apps.exports.models import ExportJob
        from apps.operations.models import AuditEvent
        from apps.patients.models import Patient, PatientInvitation, PatientMembership, PatientShare

        assert Patient.objects.get(pk=patient.pk).account_id == account.pk
        current = PatientMembership.objects.get(pk=member.pk)
        assert current.revision == 7 and current.role == "ADMIN"
        current_job = ExportJob.objects.get(pk=job.pk)
        assert current_job.requested_by_id == account.pk and current_job.access_revision == 7
        assert current_job.session_digest == "a" * 64 and current_job.snapshot == {"synthetic": "preserved"}
        current_audit = AuditEvent.objects.get(pk=audit.pk)
        assert {field: getattr(current_audit, field) for field in expected} == expected
        assert current_audit.actor_kind == "legacy" and current_audit.patient_hash == ""
        assert not PatientInvitation.objects.exists() and not PatientShare.objects.exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
