import hashlib
import io
import json

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image
from pypdf import PdfWriter
import pytest

from apps.documents.models import Document, DocumentStatus, ProcessingRun, UploadBatch, UploadItem
from apps.patients.services import create_patient_space
from apps.processing.runner import ExecutionState, PipelineResult, run_processing
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db(transaction=True)
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "upload-acceptance-test"}


@pytest.fixture(autouse=True)
def clear_upload_rate_cache():
    cache.clear()


def _authenticated_patient(django_user_model, marker):
    account = django_user_model.objects.create(
        phone_hash=hashlib.sha256(marker.encode()).hexdigest(),
        phone_encrypted="synthetic-ciphertext",
    )
    patient = create_patient_space(account, "验收测试患者", CONFIRMATIONS, EVIDENCE)
    client = Client()
    client.force_login(account)
    return client, account, patient


def _png_bytes(color="#718096"):
    output = io.BytesIO()
    Image.new("RGB", (18, 12), color).save(output, format="PNG")
    return output.getvalue()


def _pdf_bytes(marker, page_count=3):
    output = io.BytesIO()
    writer = PdfWriter()
    for page_number in range(page_count):
        writer.add_blank_page(width=612 + marker, height=792 + page_number)
    writer.add_metadata({"/SyntheticFixture": f"fixture-{marker}"})
    writer.write(output)
    return output.getvalue()


def _create_batch(client, files):
    response = client.post(
        "/api/upload-batches/",
        data=json.dumps(
            {
                "files": [
                    {"name": name, "byte_size": len(payload)}
                    for name, payload, _content_type in files
                ]
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201
    return response.json()


def _upload_reserved(client, batch_id, item_id, name, payload, content_type):
    return client.post(
        f"/api/upload-batches/{batch_id}/items/{item_id}/content/",
        {"file": SimpleUploadedFile(name, payload, content_type=content_type)},
    )


def _upload_one(client, name, payload, content_type):
    batch = _create_batch(client, [(name, payload, content_type)])
    item_id = batch["items"][0]["item_id"]
    response = _upload_reserved(client, batch["batch_id"], item_id, name, payload, content_type)
    return batch["batch_id"], item_id, response


def _stream_body(response):
    return b"".join(response.streaming_content)


def test_ac02_accepts_exactly_twenty_files_and_sixty_pages(django_user_model, monkeypatch):
    client, _account, patient = _authenticated_patient(django_user_model, "ac02")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    files = [
        (f"synthetic-{index:02d}.pdf", _pdf_bytes(index), "application/pdf")
        for index in range(1, 21)
    ]

    batch_payload = _create_batch(client, files)
    responses = []
    for server_item, (name, payload, content_type) in zip(batch_payload["items"], files, strict=True):
        responses.append(
            _upload_reserved(
                client,
                batch_payload["batch_id"],
                server_item["item_id"],
                name,
                payload,
                content_type,
            )
        )

    assert all(response.status_code == 201 and response.json()["saved"] is True for response in responses)
    batch = UploadBatch.objects.get(pk=batch_payload["batch_id"], patient=patient)
    assert (batch.file_count, batch.page_count) == (20, 60)
    assert Document.objects.filter(patient=patient).count() == 20
    assert sum(Document.objects.filter(patient=patient).values_list("page_count", flat=True)) == 60
    assert len(store.objects) == 20


def test_ac03_durable_save_survives_leaving_page_and_a_new_session_can_track_and_open_original(
    django_user_model, settings, tmp_path
):
    client, account, patient = _authenticated_patient(django_user_model, "ac03")
    settings.DOCUMENT_STORAGE_BACKEND = "local"
    settings.DOCUMENT_STORAGE_ROOT = tmp_path / "private-objects"
    payload = _png_bytes()

    batch_id, _item_id, uploaded = _upload_one(client, "synthetic-leave.png", payload, "image/png")
    assert uploaded.status_code == 201 and uploaded.json()["saved"] is True
    document = Document.objects.get(pk=uploaded.json()["document_id"], patient=patient)
    assert (settings.DOCUMENT_STORAGE_ROOT / document.original_object_key).read_bytes() == payload
    assert ProcessingRun.objects.filter(document=document).exists()

    client.logout()
    returned = Client()
    returned.force_login(account)
    status = returned.get(f"/api/upload-batches/{batch_id}/status/")
    summary = returned.get(f"/records/{document.pk}/")
    original = returned.get(f"/records/{document.pk}/original/")

    assert status.status_code == 200
    assert status.json()["items"][0]["document_id"] == str(document.pk)
    assert summary.status_code == 200
    assert original.status_code == 200
    assert original["Content-Type"] == "image/png"
    assert _stream_body(original) == payload


def test_ac04_failed_upload_creates_no_document_and_same_item_can_retry(django_user_model, monkeypatch):
    client, _account, patient = _authenticated_patient(django_user_model, "ac04")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    name = "synthetic-retry.png"
    batch = _create_batch(client, [(name, _png_bytes(), "image/png")])
    item_id = batch["items"][0]["item_id"]

    failed = _upload_reserved(client, batch["batch_id"], item_id, name, b"not-an-image", "image/png")
    assert failed.status_code == 400
    assert not Document.objects.filter(patient=patient).exists()
    assert UploadItem.objects.get(pk=item_id).document_id is None

    payload = _png_bytes("#d97706")
    retried = _upload_reserved(client, batch["batch_id"], item_id, name, payload, "image/png")
    assert retried.status_code == 201 and retried.json()["saved"] is True
    document = Document.objects.get(pk=retried.json()["document_id"], patient=patient)
    assert store.objects[document.original_object_key] == payload


def test_ac05_parsing_downgrade_keeps_original_readable(django_user_model, monkeypatch):
    client, _account, patient = _authenticated_patient(django_user_model, "ac05")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    payload = _png_bytes("#2563eb")
    _batch_id, _item_id, uploaded = _upload_one(client, "synthetic-original-only.png", payload, "image/png")
    document = Document.objects.get(pk=uploaded.json()["document_id"], patient=patient)
    run = ProcessingRun.objects.get(document=document)

    result = run_processing(run.pk, lambda _context: PipelineResult.original_only())
    document.refresh_from_db()
    original = client.get(f"/records/{document.pk}/original/")

    assert result.state == ExecutionState.NO_STRUCTURED_RESULT
    assert document.status == DocumentStatus.ORIGINAL_ONLY
    assert original.status_code == 200
    assert _stream_body(original) == payload


def test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder(
    django_user_model, monkeypatch
):
    client, _account, patient = _authenticated_patient(django_user_model, "ac06-ac07")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    payload = _png_bytes("#16a34a")
    _first_batch, _first_item, created = _upload_one(client, "synthetic-first.png", payload, "image/png")
    _second_batch, _second_item, duplicate = _upload_one(client, "synthetic-copy.png", payload, "image/png")

    assert duplicate.status_code == 200
    assert duplicate.json()["outcome"] == "EXACT_DUPLICATE"
    assert duplicate.json()["document_id"] == created.json()["document_id"]
    assert Document.objects.filter(patient=patient).count() == 1

    document_id = duplicate.json()["document_id"]
    summary = client.get(f"/records/{document_id}/")
    content = summary.content.decode()
    assert summary.status_code == 200
    assert "日期未识别" in content
    assert "查看原件" in content
    assert "required" not in content
    original = client.get(f"/records/{document_id}/original/")
    assert original.status_code == 200
    assert _stream_body(original) == payload
