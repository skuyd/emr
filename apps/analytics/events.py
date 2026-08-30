from dataclasses import dataclass
import hashlib
import hmac
import uuid

from django.conf import settings

from .models import ProductEvent


class InvalidProductEvent(ValueError):
    pass


@dataclass(frozen=True)
class FieldRule:
    kind: type
    choices: frozenset | None = None
    minimum: int | None = None
    maximum: int | None = None


def _enum(*values):
    return FieldRule(str, choices=frozenset(values))


def _integer(minimum, maximum):
    return FieldRule(int, minimum=minimum, maximum=maximum)


BOOL = FieldRule(bool)
DURATION = _enum("lt_1s", "1_3s", "3_10s", "10_60s", "gte_60s", "unknown")
SIZE = _enum("lt_1mb", "1_10mb", "10_50mb", "50_100mb", "gte_100mb", "unknown")
COUNT = _enum("0", "1_5", "6_20", "21_100", "101_300", "gt_300")
DOCUMENT_TYPE = _enum("LAB", "IMAGING", "PATHOLOGY", "DISCHARGE", "ORDER", "TREATMENT", "OTHER", "UNKNOWN")
DOCUMENT_STATUS = _enum("PROCESSING", "ORGANIZED", "ORIGINAL_ONLY", "PROCESSING_FAILED")
FORMAT = _enum("jpeg", "png", "heic", "pdf")


EVENT_SCHEMAS = {
    "login_succeeded": {"is_first_login": BOOL, "duration_bucket": DURATION},
    "patient_created": {"duration_bucket": DURATION},
    "upload_entry_clicked": {"source": _enum("home", "records", "other")},
    "files_selected": {
        "file_count": _integer(1, 20),
        "page_count": _integer(1, 60),
        "jpeg_count": _integer(0, 20),
        "png_count": _integer(0, 20),
        "heic_count": _integer(0, 20),
        "pdf_count": _integer(0, 20),
    },
    "upload_started": {"file_count": _integer(1, 20), "total_size_bucket": SIZE},
    "file_upload_succeeded": {"format": FORMAT, "size_bucket": SIZE, "duration_bucket": DURATION},
    "file_upload_failed": {
        "error_type": _enum("format", "size", "pages", "corrupt", "network", "quota", "storage", "other")
    },
    "processing_finished": {
        "final_status": DOCUMENT_STATUS,
        "duration_bucket": DURATION,
        "field_count_bucket": COUNT,
    },
    "browser_notification_enabled": {
        "browser_family": _enum("chrome", "edge", "safari", "other", "unknown"),
        "authorization_result": _enum("granted", "denied", "disabled"),
    },
    "archive_viewed": {"document_count_bucket": COUNT},
    "search_submitted": {"query_length": _integer(0, 100), "result_count_bucket": COUNT},
    "search_result_opened": {"result_position": _integer(1, 300), "document_type": DOCUMENT_TYPE},
    "document_opened": {"document_type": DOCUMENT_TYPE, "processing_status": DOCUMENT_STATUS},
    "original_opened": {
        "source": _enum("detail", "viewer", "evidence", "other"),
        "page_count_bucket": COUNT,
    },
    "evidence_opened": {"located": BOOL},
    "trend_opened": {"point_count": _integer(2, 300)},
    "inaccurate_feedback": {
        "document_type": DOCUMENT_TYPE,
        "field_category": _enum("document", "indicator"),
    },
    "product_feedback": {"category": _enum("general")},
    "document_deleted": {"document_type": DOCUMENT_TYPE},
    "account_deleted": {"usage_days_bucket": _enum("0", "1_7", "8_30", "31_90", "gt_90")},
    "return_visit": {"days_since_first_upload_bucket": _enum("0", "1_7", "8_30", "31_90", "gt_90")},
}


def _validate_value(name, value, rule):
    if rule.kind is bool:
        if type(value) is not bool:
            raise InvalidProductEvent(f"{name} must be boolean")
    elif rule.kind is int:
        if type(value) is not int:
            raise InvalidProductEvent(f"{name} must be integer")
    elif not isinstance(value, rule.kind):
        raise InvalidProductEvent(f"{name} has an invalid type")
    if rule.choices is not None and value not in rule.choices:
        raise InvalidProductEvent(f"{name} has an invalid value")
    if rule.minimum is not None and value < rule.minimum:
        raise InvalidProductEvent(f"{name} is below the allowed range")
    if rule.maximum is not None and value > rule.maximum:
        raise InvalidProductEvent(f"{name} is above the allowed range")


def validate_product_event(name, properties):
    schema = EVENT_SCHEMAS.get(name)
    if schema is None or not isinstance(properties, dict) or set(properties) != set(schema):
        raise InvalidProductEvent("Unknown event or property set")
    for key, rule in schema.items():
        _validate_value(key, properties[key], rule)
    return dict(properties)


def _actor_hash(account_id):
    if account_id is None:
        return ""
    try:
        canonical = str(uuid.UUID(str(account_id)))
    except (TypeError, ValueError, AttributeError):
        raise InvalidProductEvent("Actor identifier must be a UUID") from None
    key = settings.ANALYTICS_HASH_KEY.encode("utf-8")
    return hmac.new(key, f"phr-analytics-v1:{canonical}".encode("ascii"), hashlib.sha256).hexdigest()


def record_product_event(name, properties, *, account_id=None):
    clean_properties = validate_product_event(name, properties)
    return ProductEvent.objects.create(
        name=name,
        actor_hash=_actor_hash(account_id),
        properties=clean_properties,
    )


def count_bucket(value):
    if value <= 0:
        return "0"
    if value <= 5:
        return "1_5"
    if value <= 20:
        return "6_20"
    if value <= 100:
        return "21_100"
    if value <= 300:
        return "101_300"
    return "gt_300"


def size_bucket(byte_size):
    if byte_size < 1024**2:
        return "lt_1mb"
    if byte_size < 10 * 1024**2:
        return "1_10mb"
    if byte_size < 50 * 1024**2:
        return "10_50mb"
    if byte_size < 100 * 1024**2:
        return "50_100mb"
    return "gte_100mb"


def duration_bucket(seconds):
    if seconds is None:
        return "unknown"
    if seconds < 1:
        return "lt_1s"
    if seconds < 3:
        return "1_3s"
    if seconds < 10:
        return "3_10s"
    if seconds < 60:
        return "10_60s"
    return "gte_60s"


def days_bucket(days):
    if days <= 0:
        return "0"
    if days <= 7:
        return "1_7"
    if days <= 30:
        return "8_30"
    if days <= 90:
        return "31_90"
    return "gt_90"
