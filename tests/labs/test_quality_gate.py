from dataclasses import replace
from datetime import date

import pytest

from apps.labs.extraction import extract_observations
from apps.labs.models import LabObservation
from apps.labs.trends import trend_view
from apps.processing.metadata import extract_document_metadata
from apps.processing.models import OcrBlock, DocumentType
from apps.processing.runner import run_processing
from tests.labs.test_extraction import _page, _region
from tests.labs.test_trends import _observation
from tests.processing.test_pipeline import _document_and_run, _ocr_page, _pipeline, _png_bytes, _Store


def _row(confidence):
    return _page(
        _region("WBC 白细胞", 0.05, 0.30, 1, confidence=confidence),
        _region("4.20", 0.40, 0.50, 2, confidence=confidence),
        _region("10^9/L", 0.55, 0.68, 3, confidence=confidence),
    )


@pytest.mark.parametrize("confidence", [0.10, 0.7999])
def test_low_confidence_row_retains_no_structured_observation(confidence):
    assert extract_observations((_row(confidence),)) == ()


@pytest.mark.parametrize("confidence", [0.80, 0.8999])
def test_uncertain_name_preserves_raw_label_instead_of_canonical_name(confidence):
    observation, = extract_observations((_row(confidence),))
    assert observation.raw_value == "4.20"
    assert observation.standard_name == observation.raw_name


def test_standard_name_is_available_at_mapping_threshold():
    observation, = extract_observations((_row(0.90),))
    assert observation.standard_name == "白细胞计数"


@pytest.mark.django_db(transaction=True)
def test_pipeline_preserves_low_confidence_ocr_without_publishing_lab_fields(django_user_model):
    document, run = _document_and_run(django_user_model)
    page = _ocr_page()
    low_confidence_page = replace(page, regions=tuple(replace(row, confidence=0.10) for row in page.regions))

    run_processing(run.pk, _pipeline(_Store(_png_bytes()), low_confidence_page))

    assert OcrBlock.objects.filter(parsing_version__document=document).count() == 6
    assert not LabObservation.objects.filter(parsing_version__document=document).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("confidence", [None, "0.1000", "0.9499"])
def test_historical_uncertain_results_cannot_enter_trends(django_user_model, confidence):
    document, _run = _document_and_run(django_user_model)
    _first_document, first = _observation(document.patient, date(2026, 8, 1), "4.2")
    _second_document, second = _observation(document.patient, date(2026, 8, 2), "4.6")
    for observation in (first, second):
        observation.evidence.confidence = confidence
        observation.evidence.save(update_fields=["confidence"])

    assert trend_view(document.patient, "LAB_WBC") is None


def test_low_confidence_date_cannot_become_the_selected_report_date():
    page = _ocr_page()
    uncertain_date = replace(page, regions=tuple(
        replace(row, confidence=0.10) if "2026-08-20" in row.text else row
        for row in page.regions
    ))
    metadata = extract_document_metadata((uncertain_date,), observation_count=1)

    assert metadata.document_date is None


def test_low_confidence_type_keyword_cannot_become_confident_metadata():
    page = _ocr_page()
    noisy_header = replace(page, regions=(replace(page.regions[0], text="病理报告", confidence=0.10),))
    metadata = extract_document_metadata((noisy_header,))
    assert metadata.document_type != DocumentType.PATHOLOGY
