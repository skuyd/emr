"""Material suggestions are separate from OCR status and retain recoverable sources."""

from django.test import Client
from django.utils import timezone
import pytest

from apps.documents.models import Document, DocumentStatus, ProcessingRun, ProcessingStage, UploadItem, UploadItemStatus
from apps.processing.models import DocumentSummary, DocumentType, MaterialDecision, ParsingVersion, ParsingVersionStatus
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db(transaction=True)


def material_document(django_user_model, marker="material-view", *, automatic="NON_DOCUMENT"):
    client, patient = _patient(django_user_model, marker)
    document, pages = _document(patient, content_type="image/png", page_count=1, status=DocumentStatus.ORIGINAL_ONLY)
    run = ProcessingRun.objects.create(
        document=document, parser_version="parser-v1", task_type="initial",
        idempotency_key=f"{document.pk}:initial", stage=ProcessingStage.NO_STRUCTURED_RESULT,
        finished_at=timezone.now(), is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="parser-v1",
        ocr_provider="fixture", ocr_provider_version="1.0", dictionary_version="1.0.0",
        dictionary_hash="a" * 64, status=ParsingVersionStatus.READY,
        diagnostics={"material": {
            "version": "material-1", "status": automatic, "source_sha256": document.sha256,
            "pages": [{"page_number": 1, "status": automatic, "precision": "page", "reason_codes": []}],
        }},
    )
    ParsingVersion.objects.activate(version)
    DocumentSummary.objects.create(parsing_version=version, document_type=DocumentType.UNKNOWN, confidence=0)
    UploadItem.objects.create(
        batch=document.batch, ordinal=1, document=document, display_filename=document.display_filename,
        byte_size=document.byte_size, page_count=1, status=UploadItemStatus.CREATED,
    )
    return client, document, version


def test_material_hint_agrees_in_batch_list_tasks_and_detail(django_user_model):
    client, document, version = material_document(django_user_model)
    result = client.get(f"/api/upload-batches/{document.batch_id}/status/")
    assert result.status_code == 200
    item = result.json()["items"][0]
    assert item["status"] == "ORIGINAL_ONLY" and item["error_code"] is None
    assert item.get("material", {}).get("automatic_status") == "NON_DOCUMENT"
    assert item["material"]["label"] == "可能不是单据"
    for url in ("/records/", "/tasks/", f"/records/{document.pk}/"):
        response = client.get(url)
        assert response.status_code == 200
        assert "可能不是单据" in response.content.decode()
    detail = client.get(f"/records/{document.pk}/").content.decode()
    assert "按资料保留并重新整理" in detail
    assert f"/records/{document.pk}/viewer/?page=1" in detail
    assert "原件已完整保存" in detail
    assert DocumentType.UNKNOWN.label in detail


def test_keep_post_is_idempotent_updates_etag_and_keeps_append_only_history(django_user_model, monkeypatch):
    client, document, version = material_document(django_user_model)
    queued = []
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", queued.append)
    status_url = f"/api/upload-batches/{document.batch_id}/status/"
    etag = client.get(status_url)["ETag"]
    data = {"action": "KEEP_DOCUMENT", "expected_version": str(version.pk), "expected_revision": "0", "patient_id": str(document.patient_id)}
    url = f"/records/{document.pk}/material/"
    response = client.post(url, data)
    assert response.status_code == 302
    assert client.post(url, data).status_code == 302
    assert MaterialDecision.objects.filter(document=document).count() == 1
    assert len(queued) == 1
    changed = client.get(status_url, HTTP_IF_NONE_MATCH=etag)
    assert changed.status_code == 200
    assert changed.json()["items"][0]["material"]["label"] == "已按资料保留"
    html = client.get(response["Location"]).content.decode()
    assert "恢复自动判断" in html and "保留方式记录" in html
    assert "原件已完整保存" in html


def test_material_post_rejects_stale_other_patient_and_csrf_without_mutation(django_user_model, monkeypatch):
    client, document, version = material_document(django_user_model)
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", lambda _: None)
    url = f"/records/{document.pk}/material/"
    data = {"action": "KEEP_DOCUMENT", "expected_version": str(version.pk), "expected_revision": "99", "patient_id": str(document.patient_id)}
    assert client.post(url, data).status_code == 409
    assert client.get(url).status_code == 405
    other, _ = _patient(django_user_model, "other-material-view")
    assert other.post(url, data).status_code == 404
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(document.patient.account)
    assert csrf.post(url, data).status_code == 403
    assert not MaterialDecision.objects.exists()
    assert ProcessingRun.objects.filter(document=document).count() == 1


def test_missing_and_uncertain_evidence_never_claims_non_document(django_user_model):
    client, document, version = material_document(django_user_model, automatic="UNCERTAIN")
    html = client.get(f"/records/{document.pk}/").content.decode()
    assert "暂无法判断是否为单据" in html and "可能不是单据" not in html
    ParsingVersion.objects.filter(pk=version.pk).update(diagnostics={})
    html = client.get(f"/records/{document.pk}/").content.decode()
    assert "按资料保留并重新整理" not in html and "可能不是单据" not in html
