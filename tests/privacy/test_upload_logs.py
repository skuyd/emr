import hashlib
import io
import json
import logging

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image, PngImagePlugin
import pytest

from apps.documents.models import ProcessingRun
from apps.patients.services import create_patient_space
from apps.processing.runner import ExecutionState, run_processing
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db(transaction=True)
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "upload-privacy-test"}


def _private_png(secret_text):
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Description", secret_text)
    Image.new("RGB", (18, 12), "#64748b").save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def _reserve_and_upload(client, name, payload):
    batch = client.post(
        "/api/upload-batches/",
        data=json.dumps({"files": [{"name": name, "byte_size": len(payload)}]}),
        content_type="application/json",
    ).json()
    response = client.post(
        f"/api/upload-batches/{batch['batch_id']}/items/{batch['items'][0]['item_id']}/content/",
        {"file": SimpleUploadedFile(name, payload, content_type="image/png")},
    )
    return batch, response


def test_upload_duplicate_cleanup_and_pipeline_error_logs_contain_no_medical_content(
    django_user_model, monkeypatch, caplog
):
    forbidden = {
        "敏感患者称呼-不得记录",
        "病理报告-张某-13800138000.png",
        "OCR秘密标记-HGB-87-阳性",
        "13800138000",
        "外部解析错误包含原始病历",
    }
    account = django_user_model.objects.create(
        phone_hash=hashlib.sha256(b"privacy-account").hexdigest(),
        phone_encrypted="synthetic-ciphertext",
    )
    create_patient_space(account, "敏感患者称呼-不得记录", CONFIRMATIONS, EVIDENCE)
    client = Client()
    client.force_login(account)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    payload = _private_png("OCR秘密标记-HGB-87-阳性")
    caplog.set_level(logging.WARNING)

    _batch, created = _reserve_and_upload(client, "病理报告-张某-13800138000.png", payload)
    assert created.status_code == 201
    store.fail_delete = True
    _duplicate_batch, duplicate = _reserve_and_upload(client, "病理报告-张某-13800138000.png", payload)
    assert duplicate.status_code == 200
    run = ProcessingRun.objects.get(document_id=created.json()["document_id"])

    def unsafe_failure(_context):
        raise RuntimeError("外部解析错误包含原始病历")

    result = run_processing(run.pk, unsafe_failure)
    assert result.state == ExecutionState.RETRY_SCHEDULED

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "staging_cleanup_deferred" in rendered_logs
    assert "Processing pipeline raised an untyped exception" in rendered_logs
    assert all(value not in rendered_logs for value in forbidden)
    response_text = created.content.decode() + duplicate.content.decode()
    assert all(value not in response_text for value in forbidden)
