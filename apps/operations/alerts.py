from dataclasses import dataclass

from django.conf import settings

from .metrics import collect_metric_snapshot


@dataclass(frozen=True)
class OperationalAlert:
    code: str
    severity: str
    value: float


def evaluate_alerts(snapshot=None):
    snapshot = collect_metric_snapshot() if snapshot is None else tuple(snapshot)
    alerts = []
    queue = sum(
        sample.value for sample in snapshot if sample.name == "phr_processing_queue_length"
    )
    deletion = sum(sample.value for sample in snapshot if sample.name == "phr_deletion_backlog")
    providers = sum(sample.value for sample in snapshot if sample.name == "phr_provider_error_total")
    if queue > settings.OPERATIONS_ALERT_QUEUE_THRESHOLD:
        alerts.append(OperationalAlert("processing_queue_high", "warning", queue))
    if deletion > settings.OPERATIONS_ALERT_DELETION_THRESHOLD:
        alerts.append(OperationalAlert("deletion_backlog_high", "critical", deletion))
    if providers > settings.OPERATIONS_ALERT_PROVIDER_ERROR_THRESHOLD:
        alerts.append(OperationalAlert("provider_errors_high", "warning", providers))
    return tuple(alerts)
