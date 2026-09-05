import re

from django.db import connection
from django.utils import timezone
import pytest

from apps.documents.models import DocumentStatus, ProcessingStage, UploadBatch
from apps.operations.permissions import Role
from apps.operations.services import activate_parsing_version, requeue_processing
from apps.processing.models import ParsingVersion, ParsingVersionStatus
from tests.integration.test_processing_postgres_concurrency import _document_and_run, _patient
from tests.operations.test_services import staff


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.mark.parametrize("operation", ["requeue", "activate"])
def test_operator_processing_actions_lock_aggregate_before_runs_and_versions(django_user_model, operation):
    if connection.vendor != "postgresql":
        pytest.skip("Requires the disposable PostgreSQL integration database")
    operator = staff(django_user_model, Role.PROCESSOR_OPERATOR)
    patient = _patient(django_user_model)
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document, run = _document_and_run(patient, batch, 1)
    document.status = DocumentStatus.PROCESSING_FAILED
    document.save(update_fields=["status"])
    run.stage = ProcessingStage.FAILED if operation == "requeue" else ProcessingStage.SUCCEEDED
    run.finished_at = timezone.now()
    run.save(update_fields=["stage", "finished_at"])
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="postgres-v1",
        ocr_provider="synthetic", ocr_provider_version="1", status=ParsingVersionStatus.READY,
    )
    locks = []

    def capture(execute, sql, params, many, context):
        if "FOR UPDATE" in sql:
            match = re.search(r'FROM "([a-z_]+)"', sql)
            if match:
                locks.append((match.group(1), sql))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        if operation == "requeue":
            result = requeue_processing(operator, run.pk, reason_code="operator_retry", dispatch=lambda value: None)
            assert result.attempt_number == 2
        else:
            result = activate_parsing_version(
                operator, version.pk, reason_code="verified_version", totp_verified_at=timezone.now(),
            )
            assert result.active is True

    tables = [table for table, sql in locks]
    assert tables[:3] == ["documents_uploadbatch", "documents_document", "documents_processingrun"]
    if operation == "activate":
        assert tables.index("processing_parsingversion") > tables.index("documents_processingrun")
    assert all("JOIN" not in sql for table, sql in locks)
