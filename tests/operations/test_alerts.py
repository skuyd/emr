from django.test import override_settings

from apps.operations.alerts import evaluate_alerts
from apps.operations.metrics import MetricSample


@override_settings(
    OPERATIONS_ALERT_QUEUE_THRESHOLD=2,
    OPERATIONS_ALERT_DELETION_THRESHOLD=1,
    OPERATIONS_ALERT_PROVIDER_ERROR_THRESHOLD=3,
)
def test_alerts_use_fixed_codes_and_numeric_aggregates_only():
    snapshot = (
        MetricSample("phr_processing_queue_length", {"stage": "QUEUED"}, 3),
        MetricSample("phr_deletion_backlog", {"kind": "document"}, 2),
        MetricSample(
            "phr_provider_error_total",
            {"provider": "ocr", "error_type": "timeout"},
            4,
        ),
    )

    alerts = evaluate_alerts(snapshot)

    assert [(item.code, item.severity) for item in alerts] == [
        ("processing_queue_high", "warning"),
        ("deletion_backlog_high", "critical"),
        ("provider_errors_high", "warning"),
    ]
    assert all(set(vars(item)) == {"code", "severity", "value"} for item in alerts)
