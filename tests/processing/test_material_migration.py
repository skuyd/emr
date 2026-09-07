"""Existing files and published OCR retain their identity across additive migration."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest

from apps.documents.models import Document, DocumentPage
from apps.processing.material_review import material_state
from apps.processing.models import ParsingVersion
from tests.processing.test_material_recovery import photo_document


@pytest.mark.django_db(transaction=True)
def test_existing_originals_and_versions_remain_unassessed_after_material_migration(django_user_model):
    document, version, _store, _provider = photo_document(django_user_model)
    identity = (document.sha256, document.original_object_key, document.byte_size, document.page_count)
    executor = MigrationExecutor(connection)
    try:
        targets = [("documents", "0007_processingrun_access_revision_and_more"), ("processing", "0004_ocr_layout_geometry")]
        executor.migrate(targets)
        historical = executor.loader.project_state(targets).apps
        historical.get_model("processing", "ParsingVersion").objects.filter(pk=version.pk).update(
            diagnostics={"quality_policy": "legacy-preserved"},
        )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        current = Document.objects.get(pk=document.pk)
        preserved = ParsingVersion.objects.get(pk=version.pk)
        assert (current.sha256, current.original_object_key, current.byte_size, current.page_count) == identity
        assert current.material_override == "AUTO" and current.material_revision == 0
        assert preserved.active and preserved.diagnostics == {"quality_policy": "legacy-preserved"}
        assert DocumentPage.objects.filter(document=current).count() == current.page_count
        assert not current.material_decisions.exists()
        assert material_state(current, preserved)["assessed"] is False
        assert material_state(current, preserved)["label"] == ""
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
