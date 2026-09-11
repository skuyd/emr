"""A worker retry may rebuild only its unreviewed, unpublished source graph."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import uuid

import pytest

from apps.documents.models import ProcessingRun
from apps.facts.models import FactRevision, ClinicalReportRevision
from apps.processing.errors import RetryableProcessingError
from apps.processing.models import ParsingVersion, SourceEvidence
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.facts.test_molecular_extraction import report_rows
from tests.processing.test_pipeline import _document_and_run, _png_bytes, _ocr_page, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def pipeline(*, retry=False, lose_lease=False):
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, i) for i, row in enumerate(report_rows())))
    class Pipeline(DocumentProcessingPipeline):
        once = retry
        def _persist(self, *args, **kwargs):
            super()._persist(*args, **kwargs)
            if lose_lease:
                ProcessingRun.objects.filter(pk=args[0].run_id).update(lease_token=uuid.uuid4())
            if self.once:
                self.once = False
                raise RetryableProcessingError("synthetic_after_persistence")
    return Pipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))


def snapshot(version):
    return {"reports": list(version.clinical_reports.values()), "facts": list(version.facts.values()),
            "evidence": list(SourceEvidence.objects.filter(parsing_version=version).values()),
            "ocr": list(version.ocr_blocks.values()),
            "fact_revisions": list(FactRevision.objects.filter(fact__parsing_version=version).values()),
            "report_revisions": list(ClinicalReportRevision.objects.filter(report__parsing_version=version).values())}


def test_actual_retry_replaces_unpublished_graph_but_preserves_published_history_and_original(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    from apps.facts.revisions import revise_fact
    document, first_run = _document_and_run(django_user_model)
    original = (document.sha256, document.original_object_key)
    assert run_processing(first_run.pk, pipeline()).state == ExecutionState.SUCCEEDED
    published = ParsingVersion.objects.get(processing_run=first_run)
    anchor = published.facts.filter(field_key="specimen.identity").get()
    revise_fact(document.patient, anchor.pk, actor=document.patient.account, action="CONFIRM", expected_revision=0,
                checked_original=True, expected_source=effective_field(anchor)["current_source_token"])
    before = snapshot(published)
    retry = ProcessingRun.objects.create(document=document, parser_version="retry-synthetic", task_type="synthetic",
        attempt_number=2, idempotency_key=str(uuid.uuid4()))
    worker = pipeline(retry=True)
    assert run_processing(retry.pk, worker).state == ExecutionState.RETRY_SCHEDULED
    building = ParsingVersion.objects.get(processing_run=retry)
    old_report_ids = list(building.clinical_reports.values_list("pk", flat=True))
    assert not building.active and old_report_ids
    retry.refresh_from_db()
    assert run_processing(retry.pk, worker, now=retry.next_retry_at + timedelta(seconds=1)).state == ExecutionState.SUCCEEDED
    building.refresh_from_db()
    assert building.active and building.clinical_extraction.status == "EXTRACTED"
    assert building.facts.filter(field_key="variant.allele_fraction").count() == 1
    assert not building.clinical_reports.filter(pk__in=old_report_ids).exists()
    assert snapshot(published) == before
    document.refresh_from_db()
    assert (document.sha256, document.original_object_key) == original


@pytest.mark.parametrize("changed", ["field_revision", "report_revision", "manual_field"])
def test_retry_refuses_to_destroy_audited_or_manual_unpublished_material(django_user_model, changed):
    document, run = _document_and_run(django_user_model)
    worker = pipeline(retry=True)
    assert run_processing(run.pk, worker).state == ExecutionState.RETRY_SCHEDULED
    version = ParsingVersion.objects.get(processing_run=run)
    fact = version.facts.filter(field_key="variant.allele_fraction").first()
    if changed == "field_revision":
        FactRevision.objects.create(fact=fact, author=document.patient.account, sequence=1, action="EXCLUDE",
                                    before={}, after={}, source={})
    elif changed == "report_revision":
        ClinicalReportRevision.objects.create(report=fact.clinical_report, author=document.patient.account,
            sequence=1, action="EXCLUDE", before={}, after={}, source_token="a" * 64)
    else:
        version.facts.filter(pk=fact.pk).update(origin="MANUAL", created_by=document.patient.account)
    before = deepcopy(snapshot(version))
    run.refresh_from_db()
    result = run_processing(run.pk, worker, now=run.next_retry_at + timedelta(seconds=1))
    assert result.state == ExecutionState.FAILED
    run.refresh_from_db()
    assert run.error_code == "reviewed_unpublished_version_immutable"
    assert snapshot(version) == before


def test_actual_molecular_worker_losing_lease_after_persistence_cannot_publish(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    document, first_run = _document_and_run(django_user_model)
    assert run_processing(first_run.pk, pipeline()).state == ExecutionState.SUCCEEDED
    first = ParsingVersion.objects.get(processing_run=first_run)
    before = snapshot(first)
    run = ProcessingRun.objects.create(document=document, parser_version="lease-synthetic", task_type="synthetic",
        attempt_number=2, idempotency_key=str(uuid.uuid4()))
    result = run_processing(run.pk, pipeline(lose_lease=True))
    assert result.state == ExecutionState.LEASE_LOST
    building = ParsingVersion.objects.get(processing_run=run)
    assert building.facts.filter(field_key="variant.identity").exists()
    assert not building.active
    assert all(not effective_field(f)["source_valid"] for f in building.facts.filter(representation="FIELD"))
    assert snapshot(first) == before
    first.refresh_from_db()
    assert first.active
