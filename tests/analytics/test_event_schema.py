import uuid

import pytest

from apps.analytics.events import EVENT_SCHEMAS, InvalidProductEvent, count_bucket, record_product_event
from apps.analytics.models import AppendOnlyError


pytestmark = pytest.mark.django_db


VALID_EVENTS = {
    "login_succeeded": {"is_first_login": True, "duration_bucket": "lt_1s"},
    "patient_created": {"duration_bucket": "1_3s"},
    "upload_entry_clicked": {"source": "home"},
    "files_selected": {"file_count": 2, "page_count": 3, "jpeg_count": 1, "png_count": 0, "heic_count": 0, "pdf_count": 1},
    "upload_started": {"file_count": 2, "total_size_bucket": "1_10mb"},
    "file_upload_succeeded": {"format": "pdf", "size_bucket": "1_10mb", "duration_bucket": "1_3s"},
    "file_upload_failed": {"error_type": "network"},
    "processing_finished": {"final_status": "ORGANIZED", "duration_bucket": "10_60s", "field_count_bucket": "6_20"},
    "browser_notification_enabled": {"browser_family": "chrome", "authorization_result": "granted"},
    "archive_viewed": {"document_count_bucket": "21_100"},
    "search_submitted": {"query_length": 8, "result_count_bucket": "1_5"},
    "search_result_opened": {"result_position": 1, "document_type": "LAB"},
    "document_opened": {"document_type": "LAB", "processing_status": "ORGANIZED"},
    "original_opened": {"source": "viewer", "page_count_bucket": "6_20"},
    "evidence_opened": {"located": True},
    "trend_opened": {"point_count": 2},
    "inaccurate_feedback": {"document_type": "LAB", "field_category": "document"},
    "product_feedback": {"category": "general"},
    "document_deleted": {"document_type": "OTHER"},
    "account_deleted": {"usage_days_bucket": "8_30"},
    "return_visit": {"days_since_first_upload_bucket": "1_7"},
}


def test_every_prd_event_has_an_exact_valid_closed_schema():
    assert set(VALID_EVENTS) == set(EVENT_SCHEMAS)
    actor_id = uuid.uuid4()
    for name, properties in VALID_EVENTS.items():
        event = record_product_event(name, properties, account_id=actor_id)
        assert event.properties == properties
        assert event.actor_hash != str(actor_id)
        assert len(event.actor_hash) == 64


@pytest.mark.parametrize(
    ("name", "properties"),
    [
        ("unknown_event", {}),
        ("search_submitted", {"query_length": 2, "result_count_bucket": "1_5", "query": "private query"}),
        ("document_opened", {"document_type": "LAB", "processing_status": "ORGANIZED", "filename": "private.pdf"}),
        ("trend_opened", {"point_count": {"value": 2}}),
        ("trend_opened", {"point_count": True}),
        ("search_result_opened", {"result_position": 0, "document_type": "LAB"}),
        ("document_deleted", {"document_type": "UNSUPPORTED"}),
    ],
)
def test_unknown_properties_nested_values_and_invalid_ranges_are_rejected(name, properties):
    with pytest.raises(InvalidProductEvent):
        record_product_event(name, properties)


def test_events_are_append_only():
    event = record_product_event("archive_viewed", {"document_count_bucket": "0"})
    event.properties = {"document_count_bucket": "gt_300"}
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()


def test_count_buckets_are_bounded_and_do_not_reveal_exact_large_counts():
    assert [count_bucket(value) for value in (0, 1, 6, 21, 101, 301)] == [
        "0",
        "1_5",
        "6_20",
        "21_100",
        "101_300",
        "gt_300",
    ]
