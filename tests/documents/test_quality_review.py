from datetime import date

import pytest

from apps.documents.archive import records_context
from apps.labs.trends import trend_view
from apps.processing.models import DocumentMetadataCandidate, MetadataKind, DatePrecision, SourceEvidence
from tests.documents.test_records import _patient, _record
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_archive_count_excludes_legacy_observations_below_admission_threshold(django_user_model):
    _client, patient = _patient(django_user_model, "q")
    document = _record(patient, "synthetic.pdf", observation=("WBC", "白细胞计数", "4.2", "10^9/L"))
    SourceEvidence.objects.filter(parsing_version__document=document).update(confidence="0.3000")

    context = records_context(patient, {})

    assert context["record_groups"][0].cards[0].observation_count == 0


def test_archive_cannot_promote_a_legacy_standard_name_hidden_by_detail_quality(django_user_model):
    _client, patient = _patient(django_user_model, "r")
    document = _record(patient, "synthetic.pdf", observation=("WBC", "白细胞计数", "4.2", "10^9/L"))
    SourceEvidence.objects.filter(parsing_version__document=document).update(confidence="0.8500")

    context = records_context(patient, {"q": "白细胞计数"})

    assert context["page_obj"].paginator.count == 0
    # Source text remains searchable even when the derived standard name is not.
    assert records_context(patient, {"q": "WBC"})["page_obj"].paginator.count == 1


def test_historical_low_confidence_selected_dates_cannot_supply_trend_points(django_user_model):
    _client, patient = _patient(django_user_model, "s")
    for value_date in (date(2026, 7, 1), date(2026, 8, 1)):
        _document, observation = _observation(patient, value_date, "4.2")
        candidate = DocumentMetadataCandidate.objects.get(
            parsing_version=observation.parsing_version,
            kind=MetadataKind.DOCUMENT_DATE,
            selected=True,
        )
        candidate.confidence = "0.2000"
        candidate.save(update_fields=["confidence"])
        candidate.evidence.confidence = "0.2000"
        candidate.evidence.save(update_fields=["confidence"])

    assert trend_view(patient, "LAB_WBC") is None


def test_historical_missing_date_evidence_does_not_enter_trends(django_user_model):
    _client, patient = _patient(django_user_model, "t")
    for value_date in (date(2026, 7, 1), date(2026, 8, 1)):
        _document, observation = _observation(patient, value_date, "4.2")
        DocumentMetadataCandidate.objects.filter(parsing_version=observation.parsing_version).delete()
    assert trend_view(patient, "LAB_WBC") is None


def test_known_unreliable_selected_date_is_not_a_canonical_archive_or_detail_date(django_user_model):
    from apps.documents.detail import document_detail_context, document_detail_queryset

    _client, patient = _patient(django_user_model, "u")
    document, observation = _observation(patient, date(2026, 7, 1), "4.2")
    candidate = DocumentMetadataCandidate.objects.get(parsing_version=observation.parsing_version)
    candidate.confidence = "0.2000"
    candidate.save(update_fields=["confidence"])

    card = records_context(patient, {})["record_groups"][0].cards[0]
    detail = document_detail_context(document_detail_queryset(patient).get(pk=document.pk))
    assert card.date_unknown is True
    assert card.date_value == ""
    assert detail["document_date_label"] == "日期未识别"


def test_legacy_fixed_confidence_values_do_not_prove_source_quality(django_user_model):
    _client, patient = _patient(django_user_model, "v")
    for value_date in (date(2026, 7, 1), date(2026, 8, 1)):
        _document, observation = _observation(patient, value_date, "4.2")
        version = observation.parsing_version
        version.diagnostics = {}
        version.save(update_fields=["diagnostics"])

    assert trend_view(patient, "LAB_WBC") is None
