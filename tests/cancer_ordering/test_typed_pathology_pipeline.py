"""Actual synthetic worker publication and complete typed extraction inventory."""
from dataclasses import replace

import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.cancer_ordering.sources import SourceContext
from apps.facts.models import ClinicalExtraction
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.facts.test_pathology_extraction import report_rows
from tests.processing.test_pipeline import _document_and_run, _png_bytes, _ocr_page, _Store


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize('text,count', [('肺癌', 1), ('原文未明确组织学类型', 0)])
def test_real_worker_keeps_ready_collection_identity_after_publication(django_user_model, text, count):
    document, run = _document_and_run(django_user_model)
    rows = report_rows()
    rows[4].text = '组织学诊断：' + text
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, i) for i, row in enumerate(rows)))
    observed = {}

    class Pipeline(DocumentProcessingPipeline):
        def _persist(self, context, document, *args, **kwargs):
            super()._persist(context, document, *args, **kwargs)
            version = ParsingVersion.objects.get(processing_run_id=context.run_id)
            field = version.facts.get(field_key='specimen.histology')
            source = SourceContext().fact(field.pk)
            observed.update(field_id=field.pk, input=source.input_fingerprint,
                            state=(version.status, version.active), eligible=source.source_valid,
                            collection=CollectionRun.objects.get(parsing_version=version).input_fingerprint)

    pipeline = Pipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert observed['state'] == ('READY', False) and not observed['eligible']
    assert version.active and version.status == 'PUBLISHED'
    assert SourceContext().fact(observed['field_id']).input_fingerprint == observed['input']
    scope, = SourceContext().scopes(document.patient)
    assert scope.input_fingerprint == observed['collection'] and scope.complete
    receipt = CollectionRun.objects.get(parsing_version=version)
    assert receipt.status == 'COMPLETE' and receipt.candidate_count == count
    assert CancerCandidate.objects.filter(source_fact_id=observed['field_id']).count() == count
    assert resolve_ordering(document.patient)['complete']
    assert resolve_ordering(document.patient)['profile'] == 'GENERAL'  # specimen not implicitly confirmed
    assert not version.facts.filter(revision_number__gt=0).exists()


@pytest.mark.parametrize('change', ['missing', 'failed', 'partial', 'old_rule'])
def test_incomplete_clinical_extraction_cannot_claim_complete_typed_collection(django_user_model, change):
    _, patient, _, version, _, _ = typed_fixture(django_user_model)
    extraction = ClinicalExtraction.objects.get(parsing_version=version)
    if change == 'missing':
        extraction.delete()
    elif change == 'old_rule':
        ClinicalExtraction.objects.filter(pk=extraction.pk).update(extractor_version='prior-no-typed-contract')
    else:
        ClinicalExtraction.objects.filter(pk=extraction.pk).update(status=change.upper())
    runs = collect_current(patient, actor=patient.account)
    assert runs and all(run.status == 'FAILED' for run in runs)
    assert not resolve_ordering(patient)['complete']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
