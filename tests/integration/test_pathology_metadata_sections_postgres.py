"""Excluded metadata through actual upload and normal PostgreSQL publication."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from django.db import connection

from apps.facts.clinical_context import validate_context_candidate
from apps.facts.models import Fact
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.facts.test_pathology_metadata_section_scope import section_metadata_rows
from tests.integration.test_family_postgres_concurrency import thread_call
from tests.processing.test_pipeline import _document_and_run, _ocr_page, _png_bytes, _Store


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("heading,above_title", [("质量控制", False), ("检测说明", False), ("阳性对照", True), ("送检信息", False), (None, True)])
def test_committed_upload_preserves_metadata_section_ownership(django_user_model, heading, above_title):
    if connection.vendor != "postgresql":
        pytest.skip("Requires isolated PostgreSQL")
    document, run = _document_and_run(django_user_model)
    rows = section_metadata_rows(heading, above_title=above_title)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(r.text, r.polygon, .98, i) for i,r in enumerate(rows)))
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED

    def inspect_committed():
        assert not connection.in_atomic_block
        version = ParsingVersion.objects.get(processing_run_id=run.pk)
        assert version.status == "PUBLISHED" and version.active
        score = Fact.objects.get(parsing_version=version, field_key="ihc.score")
        validate_context_candidate(score)
        for fragment in score.source_fragments.all():
            fragment.full_clean()
        return score.automatic_content

    with ThreadPoolExecutor(max_workers=1) as pool:
        actual = pool.submit(thread_call, inspect_committed).result(timeout=30)
    assert actual["value"]["values"] == ["13"]
    for binding in actual["entity_context"]["bindings"]:
        if binding["role"] in {"SPECIMEN", "ASSAY"}:
            assert binding["state"] == ("UNKNOWN" if heading in {"质量控制", "检测说明", "阳性对照"} else "BOUND")
