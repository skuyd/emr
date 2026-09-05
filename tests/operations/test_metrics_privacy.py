import math

import pytest

from apps.operations.metrics import (
    InvalidMetric,
    collect_metric_snapshot,
    record_metric,
    render_prometheus,
)


pytestmark = pytest.mark.django_db


def test_metric_schema_accepts_fixed_labels_and_rejects_identifiers_or_medical_values():
    record_metric(
        "phr_provider_error_total",
        {"provider": "webpush", "error_type": "unavailable"},
    )

    invalid = [
        ("unknown_metric", {}),
        ("phr_provider_error_total", {"provider": "webpush", "error_type": "unavailable", "patient": "x"}),
        ("phr_provider_error_total", {"provider": "FORBIDDEN_FILENAME", "error_type": "unavailable"}),
        ("phr_processing_stage_latency_seconds", {"stage": "OCR", "outcome": "987.654"}),
    ]
    for name, labels in invalid:
        with pytest.raises(InvalidMetric):
            record_metric(name, labels)
    with pytest.raises(InvalidMetric):
        record_metric(
            "phr_provider_error_total",
            {"provider": "webpush", "error_type": "unavailable"},
            value=math.inf,
        )


def test_prometheus_output_contains_only_fixed_metric_labels():
    record_metric(
        "phr_original_open_failure_total",
        {"reason": "storage_unavailable"},
        value=2,
    )

    output = render_prometheus(collect_metric_snapshot())

    assert "phr_processing_queue_length" in output
    assert "phr_deletion_backlog" in output
    assert 'reason="storage_unavailable"' in output
    for forbidden in ("patient_id", "document_id", "filename", "raw_value", "987.654"):
        assert forbidden not in output


def test_metric_insert_collision_preserves_transaction_and_both_samples(monkeypatch):
    from django.db.models.query import QuerySet
    from apps.operations.models import OperationalMetricSeries

    labels = {"provider": "ocr", "error_type": "timeout"}
    record_metric("phr_provider_error_total", labels, value=2)
    original_first = QuerySet.first

    def stale_first(queryset):
        if queryset.model is OperationalMetricSeries:
            return None  # Another writer committed after this writer's first read.
        return original_first(queryset)

    monkeypatch.setattr(QuerySet, "first", stale_first)
    result = record_metric("phr_provider_error_total", labels, value=3)

    assert result.value_sum == 5
    assert result.sample_count == 2
    assert OperationalMetricSeries.objects.count() == 1
