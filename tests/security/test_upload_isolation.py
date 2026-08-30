import hashlib
import io
import json
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image
import pytest

from apps.documents.models import Document
from apps.patients.services import create_patient_space
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db(transaction=True)
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "upload-isolation-test"}


def _client_for(django_user_model, marker):
    account = django_user_model.objects.create(
        phone_hash=hashlib.sha256(marker.encode()).hexdigest(),
        phone_encrypted="synthetic-ciphertext",
    )
    patient = create_patient_space(account, f"隔离测试-{marker}", CONFIRMATIONS, EVIDENCE)
    client = Client()
    client.force_login(account)
    return client, patient


def _png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (16, 10), "#475569").save(output, format="PNG")
    return output.getvalue()


def _upload_one(client, payload):
    batch = client.post(
        "/api/upload-batches/",
        data=json.dumps({"files": [{"name": "synthetic-private.png", "byte_size": len(payload)}]}),
        content_type="application/json",
    ).json()
    item_id = batch["items"][0]["item_id"]
    response = client.post(
        f"/api/upload-batches/{batch['batch_id']}/items/{item_id}/content/",
        {"file": SimpleUploadedFile("synthetic-private.png", payload, content_type="image/png")},
    )
    assert response.status_code == 201
    return batch["batch_id"], item_id, response.json()["document_id"]


def test_upload_status_summary_and_original_are_all_patient_scoped(django_user_model, monkeypatch):
    owner, owner_patient = _client_for(django_user_model, "owner")
    intruder, _intruder_patient = _client_for(django_user_model, "intruder")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.get_object_store", lambda: store)
    payload = _png_bytes()
    batch_id, item_id, document_id = _upload_one(owner, payload)
    document = Document.objects.get(pk=document_id, patient=owner_patient)

    cross_account = [
        intruder.get(f"/records/{document_id}/"),
        intruder.get(f"/records/{document_id}/original/"),
        intruder.get(f"/api/upload-batches/{batch_id}/status/"),
        intruder.post(f"/api/upload-batches/{batch_id}/items/{item_id}/remove/"),
        intruder.post(
            f"/api/upload-batches/{batch_id}/items/{item_id}/content/",
            {"file": SimpleUploadedFile("probe.png", payload, content_type="image/png")},
        ),
    ]
    missing_id = uuid.uuid4()
    missing_summary = intruder.get(f"/records/{missing_id}/")
    missing_original = intruder.get(f"/records/{missing_id}/original/")

    assert all(response.status_code == 404 for response in cross_account)
    assert cross_account[0].content == missing_summary.content
    assert cross_account[1].content == missing_original.content
    leaked_material = (document.display_filename, document.original_object_key, document.sha256)
    for response in cross_account:
        body = response.content.decode(errors="ignore")
        assert all(value not in body for value in leaked_material)
        assert payload not in response.content

    owner_original = owner.get(f"/records/{document_id}/original/")
    assert owner_original.status_code == 200
    assert owner_original["Content-Disposition"] == 'inline; filename="original.png"'
    assert owner_original["Cache-Control"] == "private, no-store, max-age=0"
    assert owner_original["X-Content-Type-Options"] == "nosniff"
    assert owner_original["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "sandbox" in owner_original["Content-Security-Policy"]
    serialized_headers = "\n".join(f"{name}: {value}" for name, value in owner_original.headers.items())
    assert all(value not in serialized_headers for value in leaked_material)
    assert b"".join(owner_original.streaming_content) == payload
    assert owner.get(f"/private-objects/{document.original_object_key}").status_code == 404


def test_soft_deleted_document_cannot_be_opened_even_by_owner(django_user_model, monkeypatch):
    from django.utils import timezone

    owner, owner_patient = _client_for(django_user_model, "deleted-owner")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.get_object_store", lambda: store)
    _batch_id, _item_id, document_id = _upload_one(owner, _png_bytes())
    Document.objects.filter(pk=document_id, patient=owner_patient).update(deleted_at=timezone.now())

    assert owner.get(f"/records/{document_id}/").status_code == 404
    assert owner.get(f"/records/{document_id}/original/").status_code == 404


def test_missing_private_object_returns_safe_non_cacheable_service_error(django_user_model, monkeypatch, caplog):
    import logging

    owner, owner_patient = _client_for(django_user_model, "missing-object")
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.get_object_store", lambda: store)
    _batch_id, _item_id, document_id = _upload_one(owner, _png_bytes())
    document = Document.objects.get(pk=document_id, patient=owner_patient)
    store.objects.pop(document.original_object_key)
    caplog.set_level(logging.WARNING)

    response = owner.get(f"/records/{document_id}/original/")

    assert response.status_code == 503
    assert response["Cache-Control"] == "private, no-store, max-age=0"
    assert response["X-Content-Type-Options"] == "nosniff"
    safe_body = response.content.decode()
    safe_logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (document.display_filename, document.original_object_key, document.sha256):
        assert secret not in safe_body
        assert secret not in safe_logs
