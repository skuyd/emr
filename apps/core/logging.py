import hashlib
import hmac
import logging

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
        return True


def hash_identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identifier must not be empty.")

    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
