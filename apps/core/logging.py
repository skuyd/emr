import hashlib
import hmac
import logging
from collections.abc import Mapping

from django.conf import settings


SENSITIVE_LOG_FIELDS = frozenset(
    {
        "phone",
        "patient_name",
        "filename",
        "ocr_text",
        "search_query",
        "lab_name",
        "lab_value",
    }
)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        for field in SENSITIVE_LOG_FIELDS:
            if hasattr(record, field):
                setattr(record, field, "[REDACTED]")
        if isinstance(record.args, Mapping):
            record.args = _redact_mapping(record.args)
        elif (
            isinstance(record.args, tuple)
            and len(record.args) == 1
            and isinstance(record.args[0], Mapping)
        ):
            record.args = (_redact_mapping(record.args[0]),)
        return True


def _redact_mapping(values):
    return {
        field: "[REDACTED]" if field in SENSITIVE_LOG_FIELDS else value
        for field, value in values.items()
    }


def hash_identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identifier must not be empty.")

    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
