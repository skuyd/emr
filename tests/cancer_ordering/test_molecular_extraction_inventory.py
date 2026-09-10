"""Exact extraction identities across actual molecular and typed cancer input."""
from copy import deepcopy
from dataclasses import replace

import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.services import collect_current
from apps.cancer_ordering.sources import SourceContext
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.clinical_readmodels import effective_field
from apps.facts.models import ClinicalExtraction
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.facts.test_molecular_extraction import report_rows as molecular_rows
from tests.facts.test_pathology_extraction import report_rows as pathology_rows
from tests.processing.test_pipeline import _document_and_run, _png_bytes, _ocr_page, _Store


pytestmark = pytest.mark.django_db(transaction=True)
LEGACY = "clinical-imaging-v7+pathology-ihc-v6"
COMBINED = LEGACY + "+molecular-reported-v1"


def test_current_actual_extraction_collects_typed_histology(django_user_model):
    _, patient, _, version, field, _ = typed_fixture(django_user_model, name="current-combined-inventory")
    extraction = ClinicalExtraction.objects.get(parsing_version=version)
    assert extraction.extractor_version == COMBINED
    scope, = SourceContext().scopes(patient, version=version)
    assert scope.complete
    assert all(run.status == "COMPLETE" for run in collect_current(patient, actor=patient.account))
    assert CancerCandidate.objects.get(source_fact=field).original_data["profile"] == "LUNG"


def test_published_legacy_extraction_and_existing_confirmation_are_not_rewritten(django_user_model):
    _, patient, document, version, field, anchor = typed_fixture(django_user_model, name="published-inventory")
    extraction = ClinicalExtraction.objects.get(parsing_version=version)
    ClinicalExtraction.objects.filter(pk=extraction.pk).update(extractor_version=LEGACY)
    before = ClinicalExtraction.objects.values().get(pk=extraction.pk)
    fields = deepcopy(list(document.facts.order_by("pk").values()))
    token = effective_field(anchor)["current_source_token"]
    assert effective_field(anchor)["usable"]
    assert extract_clinical_version(version).pk == extraction.pk
    assert ClinicalExtraction.objects.values().get(pk=extraction.pk) == before
    assert all(run.status == "COMPLETE" for run in collect_current(patient, actor=patient.account))
    assert CancerCandidate.objects.get(source_fact=field).original_data["profile"] == "LUNG"
    assert list(document.facts.order_by("pk").values()) == fields
    assert effective_field(anchor)["usable"] and effective_field(anchor)["current_source_token"] == token


@pytest.mark.parametrize("identity", [
    "clinical-molecular-v1",  # Never-published molecular development identity.
    "clinical-imaging-v7+molecular-reported-v1",  # Missing pathology component.
    "clinical-imaging-v7+pathology-ihc-v6+unknown-v1",
    COMBINED + "+unknown-v1",
    "pathology-ihc-v6+clinical-imaging-v7+molecular-reported-v1",
])
def test_unknown_or_incomplete_identity_cannot_claim_typed_inventory(django_user_model, identity):
    _, patient, _, version, field, _ = typed_fixture(django_user_model, name="unsupported-inventory")
    ClinicalExtraction.objects.filter(parsing_version=version).update(extractor_version=identity)
    scope, = SourceContext().scopes(patient, version=version)
    assert not scope.complete and scope.reason == "typed_extraction_incomplete"
    assert all(run.status == "FAILED" for run in collect_current(patient, actor=patient.account))
    assert not CancerCandidate.objects.filter(source_fact=field).exists()


def test_actual_worker_combines_pathology_and_molecular_without_losing_typed_candidates(django_user_model):
    document, run = _document_and_run(django_user_model)
    rows = pathology_rows()
    rows[4].text = "组织学诊断：肺癌"
    rows += molecular_rows()
    regions = tuple(OcrRegion(row.text, [[.05, .04 + i * .03], [.95, .04 + i * .03],
        [.95, .06 + i * .03], [.05, .06 + i * .03]], .98, i) for i, row in enumerate(rows))
    page = replace(_ocr_page(), regions=regions)
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == "PUBLISHED"
    extraction = ClinicalExtraction.objects.get(parsing_version=version)
    assert extraction.extractor_version == COMBINED and extraction.status == "EXTRACTED"
    assert set(version.clinical_reports.values_list("routing_kind", flat=True)) == {"PATHOLOGY", "MOLECULAR"}
    histology = version.facts.get(field_key="specimen.histology")
    metric = version.facts.get(field_key="variant.allele_fraction")
    assert metric.automatic_content["value"]["values"] == ["01.20"]
    assert metric.clinical_report_id != histology.clinical_report_id
    assert CancerCandidate.objects.get(source_fact=histology).original_data["profile"] == "LUNG"
    assert not CancerCandidate.objects.filter(source_fact__clinical_report=metric.clinical_report).exists()
    scope, = SourceContext().scopes(document.patient)
    assert scope.complete
    assert CollectionRun.objects.get(parsing_version=version).status == "COMPLETE"
    assert not version.facts.filter(revision_number__gt=0).exists()
    for fact in (histology, metric):
        for piece in fact.source_fragments.select_related("ocr_block"):
            assert piece.raw_text == piece.ocr_block.text[piece.start_offset:piece.end_offset]
