from django.db import migrations, models
from django.utils import timezone


def assign_document_attempt_numbers(apps, _schema_editor):
    processing_run = apps.get_model("documents", "ProcessingRun")
    document_ids = processing_run.objects.order_by().values_list("document_id", flat=True).distinct()
    for document_id in document_ids.iterator():
        run_ids = processing_run.objects.filter(document_id=document_id).order_by("created_at", "pk").values_list(
            "pk", flat=True
        )
        for attempt_number, run_id in enumerate(run_ids.iterator(), start=1):
            processing_run.objects.filter(pk=run_id).update(attempt_number=attempt_number)
    migration_time = timezone.now()
    processing_run.objects.filter(
        stage__in=["PREPARING", "OCR", "CLASSIFYING", "EXTRACTING", "INDEXING"]
    ).update(
        stage="QUEUED",
        lease_token=None,
        heartbeat_at=migration_time,
        next_retry_at=migration_time,
        error_code="processing_migration_requeued",
    )


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="processingrun",
            name="lease_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(assign_document_attempt_numbers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="processingrun",
            constraint=models.UniqueConstraint(
                fields=("document", "attempt_number"),
                name="documents_run_document_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingrun",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(stage="QUEUED", lease_token__isnull=True)
                    | models.Q(
                        stage__in=["PREPARING", "OCR", "CLASSIFYING", "EXTRACTING", "INDEXING"],
                        lease_token__isnull=False,
                    )
                    | models.Q(
                        stage__in=["SUCCEEDED", "NO_STRUCTURED_RESULT", "FAILED"],
                        lease_token__isnull=True,
                    )
                ),
                name="documents_run_lease_consistent",
            ),
        ),
    ]
