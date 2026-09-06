import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from tests.documents.test_detail_viewer import _document, _patient


@pytest.mark.django_db(transaction=True)
def test_existing_permanent_deletions_remain_irreversible_after_recycle_bin_migration(django_user_model):
    _, patient = _patient(django_user_model, "legacy-trash-migration")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("documents", "0004_documentdeletionjob")])
        legacy = executor.loader.project_state([("documents", "0004_documentdeletionjob")]).apps
        legacy_document = legacy.get_model("documents", "Document")
        legacy_document.objects.filter(pk=document.pk).update(deleted_at=timezone.now())
        old_job = legacy.get_model("documents", "DocumentDeletionJob").objects.create(
            document_id=document.pk, object_key=document.original_object_key,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from apps.documents.models import Document, DocumentDeletionJob
        from apps.documents.lifecycle import LifecycleUnavailable, restore_document

        restored = Document.objects.get(pk=document.pk)
        assert restored.deleted_at is not None and restored.trashed_at is None and restored.trash_expires_at is None
        assert DocumentDeletionJob.objects.filter(pk=old_job.pk, document_id=document.pk).exists()
        with pytest.raises(LifecycleUnavailable):
            restore_document(patient, document.pk)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
