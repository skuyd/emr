import pytest

from apps.labs.dictionary import load_dictionary
from apps.labs.extraction import extract_observations
from apps.labs.models import LabObservation
from apps.labs.validation import VALIDATION_RULE_VERSION
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from tests.processing.test_pipeline import _Store, _document_and_run, _ocr_page, _png_bytes


pytestmark = pytest.mark.django_db(transaction=True)


def test_pipeline_persists_field_sources_and_versioned_quality(django_user_model):
    dictionary = load_dictionary("apps/labs/dictionaries/phase-two.json")
    document, run = _document_and_run(django_user_model)
    page = _ocr_page()
    extracted = extract_observations((page,), dictionary)
    assert extracted and extracted[0].field_evidence
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()),
        raster_provider=FixtureOcrProvider((page,)), dictionary=dictionary)
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    row = LabObservation.objects.get(parsing_version=version)
    assert {key: row.field_evidence[key] for key in extracted[0].field_evidence} == extracted[0].field_evidence
    assert row.field_evidence['observation_date']['page_number'] == 1
    assert row.specimen == extracted[0].specimen
    assert row.reference_range == extracted[0].reference_range
    assert row.quality_rule_version == VALIDATION_RULE_VERSION
    assert str(row.pk) in version.diagnostics["validation"]


def test_observation_dates_are_scoped_to_each_report_page():
    from dataclasses import replace
    from apps.processing.metadata import extract_document_metadata, observation_page_contexts
    first = _ocr_page()
    second = replace(first, page_number=2, regions=tuple(replace(region, text=region.text.replace('2026-08-20', '2026-09-01')) for region in first.regions))
    pages = (first, second)
    observations = extract_observations(pages, load_dictionary('apps/labs/dictionaries/phase-two.json'))
    contexts, candidates = observation_page_contexts(pages, observations, extract_document_metadata(pages))
    assert contexts[1]['observation_date'].isoformat() == '2026-08-20'
    assert contexts[2]['observation_date'].isoformat() == '2026-09-01'
    assert contexts[2]['date_evidence']['page_number'] == 2
    assert {item.normalized_value for item in candidates if item.kind == 'DOCUMENT_DATE' and item.selected} == {'2026-08-20', '2026-09-01'}
