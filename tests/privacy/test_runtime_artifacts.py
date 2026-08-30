import json
import logging

from django.test import RequestFactory
import pytest

from apps.analytics.models import ProductEvent
from apps.core.privacy_scan import SyntheticCanary, scan_runtime_artifacts
from apps.documents.models import UploadItem
from apps.documents.throttling import check_upload_rate
from apps.labs.models import LabObservation
from apps.notifications.models import NotificationKind, TaskNotification
from apps.notifications.services import serialize_notification
from apps.operations.audit import record_audit_event
from apps.operations.metrics import render_prometheus
from apps.operations.models import AuditEvent
from apps.processing.models import SourceEvidence
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _parsed_document, _patient


pytestmark = pytest.mark.django_db


def test_synthetic_canaries_never_escape_to_runtime_artifacts(
    django_user_model,
    monkeypatch,
    caplog,
):
    canaries = (
        SyntheticCanary("patient_name", "CANARY_NAME_7F31D9"),
        SyntheticCanary("filename", "CANARY_FILENAME_8A42E0.pdf"),
        SyntheticCanary("search_query", "CANARY_QUERY_9B53F1"),
        SyntheticCanary("ocr_text", "CANARY_OCR_A064C2"),
        SyntheticCanary("indicator_name", "CANARY_INDICATOR_B175D3"),
        SyntheticCanary("indicator_value", "CANARY_VALUE_C286E4"),
        SyntheticCanary("ip_address", "198.51.100.87"),
    )
    values = {canary.label: canary.value for canary in canaries}
    client, patient = _patient(django_user_model, "w")
    patient.display_name = values["patient_name"]
    patient.save(update_fields=["display_name", "updated_at"])
    document, _first_evidence, _second_evidence = _parsed_document(patient)
    type(document).objects.filter(pk=document.pk).update(display_filename=values["filename"])
    SourceEvidence.objects.filter(parsing_version__document=document).update(
        source_text=values["ocr_text"]
    )
    LabObservation.objects.filter(parsing_version__document=document).update(
        raw_name=values["indicator_name"],
        raw_value=values["indicator_value"],
    )
    item = UploadItem.objects.create(
        batch=document.batch,
        ordinal=1,
        display_filename=values["filename"],
    )
    notification = TaskNotification.objects.create(
        patient=patient,
        batch=document.batch,
        kind=NotificationKind.COMPLETED,
    )
    record_audit_event(patient.account_id, "product_feedback_created", item.pk, "succeeded")

    # The authorized source tables intentionally retain these values.
    assert type(document).objects.get(pk=document.pk).display_filename == values["filename"]
    assert SourceEvidence.objects.filter(source_text=values["ocr_text"]).exists()
    assert LabObservation.objects.filter(
        raw_name=values["indicator_name"], raw_value=values["indicator_value"]
    ).exists()

    response = client.get("/records/", {"q": values["search_query"]})
    assert response.status_code == 200

    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.get_object_store", lambda: store)
    caplog.set_level(logging.WARNING)
    missing_original = client.get(f"/records/{document.pk}/original/")
    assert missing_original.status_code == 503

    cache_keys = []

    def capture_increment(key, _timeout):
        cache_keys.append(key)
        return 1

    monkeypatch.setattr("apps.documents.throttling._increment", capture_increment)
    request = RequestFactory().post("/api/upload-batches/", REMOTE_ADDR=values["ip_address"])
    check_upload_rate(request, patient, now=1_800_000_000)

    artifacts = {
        "application_logs": [
            {
                "message": record.getMessage(),
                "error_code": getattr(record, "error_code", ""),
            }
            for record in caplog.records
        ],
        "analytics_events": list(ProductEvent.objects.values("name", "actor_hash", "properties")),
        "audit_events": list(
            AuditEvent.objects.values("actor_hash", "action", "target_hash", "result", "reason_code")
        ),
        "notification_payload": serialize_notification(notification),
        "cache_keys": cache_keys,
        "operational_metrics": render_prometheus(),
        "error_response_headers": dict(missing_original.headers),
        "error_response_body": missing_original.content,
    }

    assert scan_runtime_artifacts(artifacts, canaries) == ()


def test_privacy_scanner_reports_artifact_and_canary_without_echoing_value():
    canary = SyntheticCanary("search_query", "CANARY_QUERY_D397F5")

    leaks = scan_runtime_artifacts({"analytics": json.dumps({"query": canary.value})}, [canary])

    assert {(leak.artifact, leak.canary) for leak in leaks} == {("analytics", "search_query")}
    assert all(canary.value not in repr(leak) for leak in leaks)
