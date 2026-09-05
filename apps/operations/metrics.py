from dataclasses import dataclass
import json
import logging
import math

from django.db import IntegrityError, transaction
from django.db.models import F, Sum

from apps.accounts.models import AccountDeletionJob
from apps.documents.models import Document, DocumentDeletionJob, DocumentStatus, ProcessingRun, ProcessingStage
from apps.notifications.models import PushDelivery, PushDeliveryStatus

from .models import OperationalMetricSeries


logger = logging.getLogger(__name__)


class InvalidMetric(ValueError):
    pass


STAGES = frozenset(
    {
        ProcessingStage.QUEUED,
        ProcessingStage.PREPARING,
        ProcessingStage.OCR,
        ProcessingStage.CLASSIFYING,
        ProcessingStage.EXTRACTING,
        ProcessingStage.INDEXING,
    }
)
METRIC_SCHEMAS = {
    "phr_processing_stage_latency_seconds": {
        "stage": STAGES,
        "outcome": frozenset({"completed", "failed", "retry"}),
    },
    "phr_provider_error_total": {
        "provider": frozenset({"ocr", "sms", "object_storage", "webpush"}),
        "error_type": frozenset({"unavailable", "timeout", "rejected", "invalid_response"}),
    },
    "phr_original_open_failure_total": {
        "reason": frozenset({"not_found", "storage_unavailable", "invalid_reference", "configuration"}),
    },
}
DERIVED_METRICS = frozenset(
    {
        "phr_processing_queue_length",
        "phr_processing_retry_total",
        "phr_index_difference",
        "phr_deletion_backlog",
    }
)
HELP = {
    "phr_processing_stage_latency_seconds": "Observed processing stage latency in seconds.",
    "phr_provider_error_total": "External provider failures grouped by fixed categories.",
    "phr_original_open_failure_total": "Original file open failures grouped by fixed reasons.",
    "phr_processing_queue_length": "Current processing runs by queued or running stage.",
    "phr_processing_retry_total": "Durable processing retry count.",
    "phr_index_difference": "Active documents without an active parsing version.",
    "phr_deletion_backlog": "Durable deletion or push jobs awaiting completion.",
}


@dataclass(frozen=True)
class MetricSample:
    name: str
    labels: dict
    value: float
    sample_count: int = 1


def _validated(name, labels, value):
    schema = METRIC_SCHEMAS.get(name)
    if schema is None or not isinstance(labels, dict) or set(labels) != set(schema):
        raise InvalidMetric("Unknown metric or label set")
    for key, choices in schema.items():
        if not isinstance(labels[key], str) or labels[key] not in choices:
            raise InvalidMetric("Metric label is outside its fixed enum")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidMetric("Metric values must be finite numbers")
    value = float(value)
    if value < 0 or value > 1_000_000_000_000:
        raise InvalidMetric("Metric value is outside its bounded range")
    return dict(sorted(labels.items())), value


def record_metric(name, labels, *, value=1):
    labels, value = _validated(name, labels, value)
    label_key = json.dumps(labels, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with transaction.atomic():
        series = OperationalMetricSeries.objects.select_for_update().filter(
            name=name,
            label_key=label_key,
        ).first()
        if series is None:
            try:
                # Roll back only the competing INSERT before reading the winner.
                # Catching IntegrityError in the outer atomic block leaves its
                # transaction broken and loses this sample.
                with transaction.atomic():
                    series = OperationalMetricSeries.objects.create(
                        name=name,
                        label_key=label_key,
                        labels=labels,
                        value_sum=value,
                        sample_count=1,
                    )
                return series
            except IntegrityError:
                series = OperationalMetricSeries.objects.select_for_update().get(
                    name=name,
                    label_key=label_key,
                )
        series.value_sum = F("value_sum") + value
        series.sample_count = F("sample_count") + 1
        series.save(update_fields=["value_sum", "sample_count", "updated_at"])
    series.refresh_from_db()
    return series


def safe_record_metric(name, labels, *, value=1):
    try:
        record_metric(name, labels, value=value)
    except Exception:
        logger.warning("Operational metric write failed", extra={"error_code": "metric_write_failed"})
        return False
    return True


def collect_metric_snapshot():
    samples = []
    for series in OperationalMetricSeries.objects.order_by("name", "label_key"):
        samples.append(
            MetricSample(
                series.name,
                dict(series.labels),
                series.value_sum,
                series.sample_count,
            )
        )
    for stage in sorted(STAGES):
        samples.append(
            MetricSample(
                "phr_processing_queue_length",
                {"stage": str(stage)},
                ProcessingRun.objects.filter(stage=stage).count(),
            )
        )
    retry_total = ProcessingRun.objects.aggregate(total=Sum("retry_count"))["total"] or 0
    samples.append(MetricSample("phr_processing_retry_total", {}, retry_total))
    index_difference = (
        Document.objects.filter(
            deleted_at__isnull=True,
            status__in=[DocumentStatus.ORGANIZED, DocumentStatus.ORIGINAL_ONLY],
        )
        .exclude(parsing_versions__active=True)
        .distinct()
        .count()
    )
    samples.append(
        MetricSample(
            "phr_index_difference",
            {"direction": "document_without_active_version"},
            index_difference,
        )
    )
    backlogs = {
        "document": DocumentDeletionJob.objects.count(),
        "account": AccountDeletionJob.objects.count(),
        "push": PushDelivery.objects.filter(
            status__in=[PushDeliveryStatus.PENDING, PushDeliveryStatus.SENDING, PushDeliveryStatus.RETRY]
        ).count(),
    }
    for kind, count in backlogs.items():
        samples.append(MetricSample("phr_deletion_backlog", {"kind": kind}, count))
    return tuple(samples)


def _format_number(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else format(value, ".12g")


def _labels(labels):
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    return "{" + rendered + "}"


def render_prometheus(snapshot=None):
    snapshot = collect_metric_snapshot() if snapshot is None else tuple(snapshot)
    lines = []
    grouped = {}
    for sample in snapshot:
        if sample.name not in HELP:
            raise InvalidMetric("Unknown metric sample")
        grouped.setdefault(sample.name, []).append(sample)
    for name in sorted(grouped):
        lines.append(f"# HELP {name} {HELP[name]}")
        lines.append(f"# TYPE {name} gauge")
        for sample in sorted(grouped[name], key=lambda item: json.dumps(item.labels, sort_keys=True)):
            if sample.name in METRIC_SCHEMAS:
                _validated(sample.name, sample.labels, sample.value)
            elif sample.name not in DERIVED_METRICS:
                raise InvalidMetric("Unknown derived metric")
            lines.append(f"{name}{_labels(sample.labels)} {_format_number(sample.value)}")
            if sample.name == "phr_processing_stage_latency_seconds":
                lines.append(
                    f"{name}_count{_labels(sample.labels)} {int(sample.sample_count)}"
                )
    return "\n".join(lines) + "\n"
