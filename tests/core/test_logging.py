import logging

import pytest
from django.test import override_settings

from apps.core.logging import SensitiveDataFilter, hash_identifier


def test_sensitive_data_filter_redacts_all_supported_extra_fields():
    record = logging.LogRecord("phr", logging.INFO, __file__, 1, "ok", (), None)
    sensitive_fields = {
        "phone": "+8613800138000",
        "patient_name": "王女士",
        "filename": "blood-test.pdf",
        "ocr_text": "haemoglobin 120",
        "search_query": "cholesterol",
        "lab_name": "LDL-C",
        "lab_value": "3.1",
    }
    for field, value in sensitive_fields.items():
        setattr(record, field, value)
    record.request_id = "request-123"

    assert SensitiveDataFilter().filter(record) is True
    assert {field: getattr(record, field) for field in sensitive_fields} == {
        field: "[REDACTED]" for field in sensitive_fields
    }
    assert record.request_id == "request-123"


@override_settings(SECRET_KEY="test-secret-key")
def test_hash_identifier_returns_hmac_sha256_for_current_secret_key():
    assert hash_identifier("+8613800138000") == (
        "a3c5a637f16da2d77fe400e7660e2240efbc2e30b300d7ec5b0f0c21c6e3eff1"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_hash_identifier_rejects_empty_values_without_echoing_them(value):
    with pytest.raises(ValueError) as exc_info:
        hash_identifier(value)

    assert "identifier" in str(exc_info.value).lower()


def test_hash_identifier_does_not_echo_whitespace_only_input_in_errors():
    value = "   "

    with pytest.raises(ValueError) as exc_info:
        hash_identifier(value)

    assert value not in str(exc_info.value)
