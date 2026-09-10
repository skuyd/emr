import hashlib
import hmac
import logging
from collections.abc import Mapping
import re

from django.conf import settings


SENSITIVE_LOG_FIELDS = frozenset(
    {
        "phone",
        "phone_number",
        "patient_name",
        "display_name",
        "filename",
        "display_filename",
        "original_filename",
        "ocr_text",
        "source_text",
        "search_query",
        "query_string",
        "lab_name",
        "raw_name",
        "lab_value",
        "raw_value",
        "result_value",
        "request_body",
        "verification_code",
        "url", "raw_url", "location", "Location", "payload", "current_url",
    }
)
_QUERY_STRING = re.compile(r"\?[^\s\"']+")
_ACCESS_URL = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)


def _sanitize_text(value):
    # Access credentials may be in the path or fragment, including an invalid
    # Origin quoted by Django before the protected view is reached.
    return _QUERY_STRING.sub("?[REDACTED]", _ACCESS_URL.sub("[REDACTED_URL]", value))


def _redact_value(field, value):
    if field in SENSITIVE_LOG_FIELDS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, tuple):
        return tuple(_redact_value("", item) for item in value)
    if isinstance(value, list):
        return [_redact_value("", item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        for field in SENSITIVE_LOG_FIELDS:
            if hasattr(record, field):
                setattr(record, field, "[REDACTED]")
        if isinstance(record.args, Mapping):
            record.args = _redact_mapping(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_value("", value) for value in record.args)
        if isinstance(record.msg, str):
            record.msg = _sanitize_text(record.msg)
        # Tracebacks can echo uploaded text or provider payloads through an
        # exception message. Production logs keep the exception class only.
        if record.exc_info:
            record.exception_class = record.exc_info[0].__name__
            record.exc_info = None
            record.exc_text = None
        return True


def _redact_mapping(values):
    return {
        field: _redact_value(field, value)
        for field, value in values.items()
    }


def hash_identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identifier must not be empty.")

    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
