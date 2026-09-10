"""Construction proof is separate from published user-visible eligibility."""
from dataclasses import replace
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.facts.clinical_context import validate_context_candidate
from apps.facts.clinical_readmodels import effective_field
from apps.facts.revisions import FactConflict, revise_fact
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.facts.test_pathology_extraction import report_rows
from tests.processing.test_pipeline import _document_and_run, _png_bytes, _ocr_page, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def actual_pipeline(django_user_model, *, probe=None):
    document, run = _document_and_run(django_user_model)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, i) for i, row in enumerate(report_rows())))
    class Pipeline(DocumentProcessingPipeline):
        def _persist(self, context, document, *args, **kwargs):
            super()._persist(context, document, *args, **kwargs)
            if probe:
                probe(context, document)
    pipeline = Pipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    return document, run, run_processing(run.pk, pipeline)


def test_actual_upload_pipeline_builds_context_before_publish_without_allowing_confirmation():
    # The model is obtained from the real Django app; the test never pre-publishes
    # a fabricated version to avoid the actual worker's construction boundary.
    from django.contrib.auth import get_user_model
    observed = {}
    def probe(context, document):
        version = ParsingVersion.objects.get(processing_run_id=context.run_id)
        observed["before"] = (version.status, version.active, version.published_at)
        observed["extraction"] = version.clinical_extraction.status
        score = version.facts.filter(field_key="ihc.score").first()
        if not score:
            return
        observed["source_valid"] = effective_field(score)["source_valid"]
        try:
            validate_context_candidate(score)
        except ValidationError:
            observed["ordinary_validation_rejected"] = True
        try:
            revise_fact(document.patient, score.pk, actor=document.patient.account, action="CONFIRM", checked_original=True,
                        expected_revision=0, expected_source=effective_field(score)["current_source_token"])
        except (FactConflict, ValidationError):
            observed["confirmation_rejected"] = True
        observed["revisions"] = score.revisions.count()
    document, run, result = actual_pipeline(get_user_model(), probe=probe)
    assert result.state == ExecutionState.SUCCEEDED
    assert observed["extraction"] == "EXTRACTED"
    assert observed["before"] == ("READY", False, None)
    assert not observed["source_valid"] and observed["ordinary_validation_rejected"] and observed["confirmation_rejected"]
    assert observed["revisions"] == 0
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == "PUBLISHED" and version.published_at
    fields = list(version.facts.filter(representation="FIELD"))
    assert len([f for f in fields if f.field_key == "ihc.score"]) == 2
    assert all(effective_field(f)["source_valid"] and effective_field(f)["status"] == "PENDING" for f in fields)
    for field in fields:
        validate_context_candidate(field)


@pytest.mark.parametrize("case", ["wrong_run", "wrong_lease", "revoked", "newer_generation", "boolean", "not_atomic"])
def test_construction_exception_needs_exact_live_processing_context(django_user_model, case):
    observed = []
    def probe(context, document):
        score = document.facts.filter(field_key="ihc.score").first()
        if score is None:
            observed.append("no_score")
            return
        if case == "wrong_run":
            supplied = replace(context, run_id=uuid.uuid4())
        elif case == "wrong_lease":
            supplied = replace(context, lease_token=uuid.uuid4())
        elif case == "boolean":
            supplied = True
        else:
            supplied = context
        if case == "not_atomic":
            # _persist has returned and this ordinary caller owns no aggregate
            # transaction; knowing a context cannot grant construction scope.
            assert not transaction.get_connection().in_atomic_block
            with pytest.raises(ValidationError):
                validate_context_candidate(score, construction_context=supplied)
        else:
            with transaction.atomic():
                if case == "revoked":
                    from apps.documents.models import ProcessingRun
                    ProcessingRun.objects.filter(pk=context.run_id).update(lease_token=uuid.uuid4())
                elif case == "newer_generation":
                    from apps.documents.models import ProcessingRun
                    from django.utils import timezone
                    ProcessingRun.objects.create(document=document, parser_version="superseding", task_type="synthetic",
                        idempotency_key=str(uuid.uuid4()), attempt_number=context.attempt_number + 1,
                        stage="FAILED", finished_at=timezone.now())
                with pytest.raises(ValidationError):
                    validate_context_candidate(score, construction_context=supplied)
                if case in {"revoked", "newer_generation"}:
                    transaction.set_rollback(True)  # Restore worker state after observing the actual database rejection.
        observed.append("rejected")
    _, _, result = actual_pipeline(django_user_model, probe=probe)
    assert result.state == ExecutionState.SUCCEEDED
    assert observed == ["rejected"]


def test_old_published_nonactive_context_remains_ineligible(django_user_model):
    from tests.facts.test_pathology_pipeline import fixture
    _, _, document, version, _ = fixture(django_user_model)
    score = version.facts.filter(field_key="ihc.score").first()
    ParsingVersion.objects.filter(pk=version.pk).update(active=False)
    with transaction.atomic():
        with pytest.raises(ValidationError):
            validate_context_candidate(score, construction_context=True)
    assert not effective_field(score)["source_valid"]
