import json
import uuid

import pytest

from apps.analytics.events import InvalidProductEvent, record_product_event


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("forbidden_key", ["query", "filename", "ocr", "phone", "patient_name", "lab_name", "raw_value"])
def test_sensitive_event_attributes_are_rejected_before_persistence(forbidden_key):
    payload = {"query_length": 9, "result_count_bucket": "1_5", forbidden_key: "UNIQUE_PRIVATE_CANARY"}
    with pytest.raises(InvalidProductEvent):
        record_product_event("search_submitted", payload, account_id=uuid.uuid4())


def test_persisted_event_contains_only_allowlisted_aggregate_values():
    event = record_product_event(
        "search_submitted",
        {"query_length": 19, "result_count_bucket": "6_20"},
        account_id=uuid.uuid4(),
    )
    serialized = json.dumps({"actor_hash": event.actor_hash, "properties": event.properties})

    assert "UNIQUE_PRIVATE_CANARY" not in serialized
    assert set(event.properties) == {"query_length", "result_count_bucket"}
