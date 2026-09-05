from concurrent.futures import ThreadPoolExecutor
import threading

from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
import pytest

from apps.operations.metrics import record_metric
from apps.operations.models import OperationalMetricSeries


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


def test_concurrent_first_metric_samples_are_both_counted(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires the disposable PostgreSQL integration database")
    first_reads = threading.Barrier(2, timeout=10)
    original_first = QuerySet.first

    def synchronized_first(queryset):
        result = original_first(queryset)
        if queryset.model is OperationalMetricSeries:
            assert result is None
            first_reads.wait()
        return result

    monkeypatch.setattr(QuerySet, "first", synchronized_first)

    def record(value):
        close_old_connections()
        try:
            record_metric("phr_provider_error_total", {"provider": "ocr", "error_type": "timeout"}, value=value)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(record, value) for value in (2, 3)]
        for future in futures:
            future.result(timeout=15)

    series = OperationalMetricSeries.objects.get()
    assert series.value_sum == 5
    assert series.sample_count == 2
