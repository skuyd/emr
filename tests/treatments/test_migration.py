"""Migration keeps existing medical and requester identities; each run is unique."""
import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_multiple_derivation_runs_receive_distinct_operations_without_identity_change():
    executor = MigrationExecutor(connection)
    target = [("treatments", "0002_remove_cyclerecordlink_treatment_record_one_source_and_more")]
    try:
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        account = apps.get_model("accounts", "Account").objects.create(phone_hash="6" * 64, phone_encrypted="synthetic")
        patient = apps.get_model("patients", "Patient").objects.create(account_id=account.pk, display_name="迁移合成患者")
        model = apps.get_model("treatments", "TreatmentDerivationRun")
        identities = []
        for index in range(2):
            run = model.objects.create(patient_id=patient.pk, rule_version="synthetic-migration", input_fingerprint=str(index) * 64,
                source_manifest_hash="a" * 64, requested_by_id=account.pk, access_revision=2, result_counts={"events": index})
            identities.append((run.pk, run.input_fingerprint, run.requested_by_id, run.result_counts))
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from apps.treatments.models import TreatmentDerivationRun
        rows = list(TreatmentDerivationRun.objects.order_by("input_fingerprint"))
        assert [(row.pk, row.input_fingerprint, row.requested_by_id, row.result_counts) for row in rows] == identities
        assert len({row.operation_id for row in rows}) == 2
        assert all(isinstance(row.operation_id, uuid.UUID) for row in rows)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
