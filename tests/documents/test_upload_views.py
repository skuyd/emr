import hashlib
import io
import json
import uuid

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.db.models import QuerySet
from django.test import Client, override_settings
from PIL import Image
import pytest

from apps.documents.models import BatchStatus, Document, ProcessingRun, UploadBatch, UploadItem, UploadItemStatus
from apps.documents.views import MAX_MULTIPART_BYTES
from apps.patients.services import create_patient_space
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db(transaction=True)
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "upload-view-test"}


@pytest.fixture(autouse=True)
def clear_upload_rate_cache():
    cache.clear()


def png_bytes(color="#718096"):
    output = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="PNG")
    return output.getvalue()


def authenticated_client(django_user_model, *, marker=None, csrf=False):
    marker = marker or uuid.uuid4().hex * 2
    account = django_user_model.objects.create(phone_hash=marker, phone_encrypted="ciphertext")
    patient = create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(account)
    return client, patient


def post_batch(client, files, *, csrf_token=None, extra=None):
    payload = {"files": files}
    if extra:
        payload.update(extra)
    headers = {"HTTP_X_CSRFTOKEN": csrf_token} if csrf_token else {}
    return client.post(
        "/api/upload-batches/",
        data=json.dumps(payload),
        content_type="application/json",
        **headers,
    )


def reserve_one(client, *, name="private-report.png", byte_size=None):
    response = post_batch(client, [{"name": name, "byte_size": byte_size or len(png_bytes())}])
    assert response.status_code == 201
    body = response.json()
    return body["batch_id"], body["items"][0]["item_id"]


def upload_path(batch_id, item_id):
    return f"/api/upload-batches/{batch_id}/items/{item_id}/content/"


def uploaded_png(name="private-report.png", color="#718096"):
    return SimpleUploadedFile(name, png_bytes(color), content_type="image/png")


def test_upload_page_and_apis_require_a_current_patient(client):
    assert client.get("/uploads/new/").status_code == 302
    assert client.post("/api/upload-batches/", data="{}", content_type="application/json").status_code == 302


def test_upload_page_uses_authenticated_shell_and_real_controls(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    response = client.get("/uploads/new/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "上传资料" in content
    assert 'type="file"' in content
    assert 'multiple' in content
    assert "开始上传" in content
    assert "aria-live=\"polite\"" in content
    assert "/static/js/upload.js" in content


def test_upload_page_has_three_explicit_steps(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="steps" * 16, phone_encrypted="ciphertext")
    create_patient_space(account, "步骤测试", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    content = client.get("/uploads/new/").content.decode()

    assert "把新资料放进健康之家" in content
    for step in ("选择文件", "确认上传", "查看保存和整理状态"):
        assert step in content
    assert "部分文件未能上传，请查看下面的文件状态" in content


def test_batch_and_file_mutations_require_csrf(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model, csrf=True)
    page = client.get("/uploads/new/")
    token = page.cookies["csrftoken"].value
    assert post_batch(client, [{"name": "one.png", "byte_size": 10}]).status_code == 403
    created = post_batch(client, [{"name": "one.png", "byte_size": len(png_bytes())}], csrf_token=token)
    body = created.json()
    path = upload_path(body["batch_id"], body["items"][0]["item_id"])
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: InMemoryObjectStore())
    assert client.post(path, {"file": uploaded_png()}).status_code == 403


def test_batch_creation_accepts_twenty_and_rejects_twenty_one_without_rows(django_user_model):
    client, patient = authenticated_client(django_user_model)
    files = [{"name": f"file-{index}.png", "byte_size": 10} for index in range(1, 21)]
    accepted = post_batch(client, files)
    assert accepted.status_code == 201
    assert len(accepted.json()["items"]) == 20
    assert UploadItem.objects.filter(batch__patient=patient).count() == 20

    rejected = post_batch(client, files + [{"name": "overflow.png", "byte_size": 10}])
    assert rejected.status_code == 400
    assert rejected.json() == {"error": {"code": "batch_file_limit"}}
    assert UploadBatch.objects.filter(patient=patient).count() == 1


def test_mixed_metadata_failures_are_per_item_and_response_never_echoes_names(django_user_model):
    client, patient = authenticated_client(django_user_model)
    secret_names = ["medical-secret.png", "diagnosis-secret.exe", "oversized-secret.pdf"]
    response = post_batch(
        client,
        [
            {"name": secret_names[0], "byte_size": 10},
            {"name": secret_names[1], "byte_size": 10},
            {"name": secret_names[2], "byte_size": 100 * 1024 * 1024 + 1},
        ],
        extra={"patient_id": str(uuid.uuid4())},
    )
    body_text = response.content.decode()
    body = response.json()
    assert response.status_code == 201
    assert [(item["accepted"], item["error_code"]) for item in body["items"]] == [
        (True, None),
        (False, "unsupported_file"),
        (False, "file_too_large"),
    ]
    assert all(name not in body_text for name in secret_names)
    assert UploadBatch.objects.get(pk=body["batch_id"]).patient_id == patient.pk
    assert list(UploadItem.objects.filter(batch_id=body["batch_id"]).values_list("status", flat=True)) == [
        UploadItemStatus.PENDING,
        UploadItemStatus.UPLOAD_FAILED,
        UploadItemStatus.UPLOAD_FAILED,
    ]


def test_all_invalid_reservations_form_a_terminal_failed_attempt(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    response = post_batch(client, [{"name": "bad.exe", "byte_size": 10}])
    batch = UploadBatch.objects.get(pk=response.json()["batch_id"])
    assert batch.status == BatchStatus.COMPLETED
    assert batch.completed_at is not None


def test_valid_file_response_marks_saved_only_after_document_and_private_object_exist(django_user_model, monkeypatch):
    client, patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    batch_id, item_id = reserve_one(client)

    response = client.post(upload_path(batch_id, item_id), {"file": uploaded_png("private-report.png")})
    body_text = response.content.decode()
    body = response.json()

    assert response.status_code == 201
    assert body["saved"] is True
    assert body["outcome"] == "CREATED"
    assert body["page_count"] == 1
    document = Document.objects.get(pk=body["document_id"], patient=patient)
    assert document.original_object_key in store.objects
    assert ProcessingRun.objects.filter(document=document, stage="QUEUED").exists()
    for forbidden in ("private-report.png", document.sha256, document.original_object_key, "medical"):
        assert forbidden not in body_text


@pytest.mark.parametrize("failure_point", ["begin", "filename"])
def test_transient_database_failure_returns_retryable_error_and_upload_can_resume(
    django_user_model, monkeypatch, failure_point
):
    client, _patient = authenticated_client(django_user_model)
    batch_id, item_id = reserve_one(client)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)

    with monkeypatch.context() as failure:
        if failure_point == "begin":
            def unavailable(*args, **kwargs):
                raise OperationalError("synthetic database contention")

            failure.setattr("apps.documents.views.uploads._begin_upload", unavailable)
        else:
            original_update = QuerySet.update

            def update(queryset, **kwargs):
                if queryset.model is UploadItem and "display_filename" in kwargs:
                    raise OperationalError("synthetic database contention")
                return original_update(queryset, **kwargs)

            failure.setattr(QuerySet, "update", update)

        response = client.post(upload_path(batch_id, item_id), {"file": uploaded_png()})

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "upload_service_unavailable"}}
    item = UploadItem.objects.get(pk=item_id)
    assert item.status in {UploadItemStatus.PENDING, UploadItemStatus.UPLOAD_FAILED}
    assert item.document_id is None
    assert not store.objects
    retry = client.post(upload_path(batch_id, item_id), {"file": uploaded_png()})
    assert retry.status_code == 201
    assert retry.json()["saved"] is True
    assert Document.objects.count() == ProcessingRun.objects.count() == 1


@override_settings(PROCESSING_DISPATCH_ON_UPLOAD=True)
def test_successful_web_upload_dispatches_only_the_new_durable_processing_run(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    dispatched = []
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.uploads.safe_enqueue_processing", lambda run_id: dispatched.append(str(run_id)))
    batch_id, item_id = reserve_one(client)

    response = client.post(upload_path(batch_id, item_id), {"file": uploaded_png()})

    assert response.status_code == 201
    run = ProcessingRun.objects.get(document_id=response.json()["document_id"])
    assert dispatched == [str(run.pk)]


def test_malformed_file_creates_no_document_and_persists_retryable_safe_failure(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model)
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: InMemoryObjectStore())
    batch_id, item_id = reserve_one(client)

    response = client.post(
        upload_path(batch_id, item_id),
        {"file": SimpleUploadedFile("private-report.png", b"broken", content_type="image/png")},
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "unsupported_file"}}
    item = UploadItem.objects.get(pk=item_id)
    assert item.status == UploadItemStatus.UPLOAD_FAILED
    assert item.error_code == "unsupported_file"
    assert item.document_id is None
    assert not Document.objects.exists()
    batch = UploadBatch.objects.get(pk=batch_id)
    assert batch.status == BatchStatus.COMPLETED


def test_failed_item_can_retry_and_reopens_completed_batch(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    batch_id, item_id = reserve_one(client)
    first = client.post(
        upload_path(batch_id, item_id),
        {"file": SimpleUploadedFile("private-report.png", b"broken", content_type="image/png")},
    )
    assert first.status_code == 400

    second = client.post(upload_path(batch_id, item_id), {"file": uploaded_png()})

    assert second.status_code == 201
    assert second.json()["saved"] is True
    item = UploadItem.objects.get(pk=item_id)
    batch = UploadBatch.objects.get(pk=batch_id)
    assert item.status == UploadItemStatus.CREATED
    assert item.error_code == ""
    assert batch.status == BatchStatus.ACTIVE
    assert batch.completed_at is None


def test_same_patient_exact_duplicate_returns_existing_only_and_cross_patient_creates(django_user_model, monkeypatch):
    first_client, first_patient = authenticated_client(django_user_model)
    second_client, second_patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)

    first_batch, first_item = reserve_one(first_client)
    created = first_client.post(upload_path(first_batch, first_item), {"file": uploaded_png()})
    duplicate_batch, duplicate_item = reserve_one(first_client)
    duplicate = first_client.post(upload_path(duplicate_batch, duplicate_item), {"file": uploaded_png()})
    second_batch, second_item = reserve_one(second_client)
    cross_patient = second_client.post(upload_path(second_batch, second_item), {"file": uploaded_png()})

    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["outcome"] == "EXACT_DUPLICATE"
    assert duplicate.json()["possible_duplicate"] is False
    assert duplicate.json()["document_id"] == created.json()["document_id"]
    assert cross_patient.status_code == 201
    assert cross_patient.json()["document_id"] != created.json()["document_id"]
    assert Document.objects.filter(patient=first_patient).count() == 1
    assert Document.objects.filter(patient=second_patient).count() == 1


def test_possible_duplicate_is_a_nonblocking_same_patient_boolean_without_identity_or_hash(
    django_user_model, monkeypatch
):
    first_client, first_patient = authenticated_client(django_user_model)
    second_client, _second_patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)

    first_batch, first_item = reserve_one(first_client)
    first = first_client.post(
        upload_path(first_batch, first_item),
        {"file": uploaded_png("first.png", "#718096")},
    )
    near_batch, near_item = reserve_one(first_client)
    near = first_client.post(
        upload_path(near_batch, near_item),
        {"file": uploaded_png("near.png", "#d97706")},
    )
    cross_batch, cross_item = reserve_one(second_client)
    cross_patient = second_client.post(
        upload_path(cross_batch, cross_item),
        {"file": uploaded_png("near.png", "#d97706")},
    )

    assert first.status_code == 201 and first.json()["possible_duplicate"] is False
    assert near.status_code == 201 and near.json()["possible_duplicate"] is True
    assert near.json()["outcome"] == "CREATED"
    assert cross_patient.status_code == 201 and cross_patient.json()["possible_duplicate"] is False
    first_document = Document.objects.get(pk=first.json()["document_id"])
    near_document = Document.objects.get(pk=near.json()["document_id"])
    assert first_document.patient_id == first_patient.pk
    assert first_document.perceptual_hash == near_document.perceptual_hash
    assert len(near_document.perceptual_hash) == 16
    assert str(first_document.pk) not in near.content.decode()
    assert near_document.perceptual_hash not in near.content.decode()


def test_foreign_batch_item_status_and_remove_are_all_404(django_user_model, monkeypatch):
    first_client, _first = authenticated_client(django_user_model)
    second_client, _second = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    batch_id, item_id = reserve_one(first_client)

    assert second_client.post(upload_path(batch_id, item_id), {"file": uploaded_png()}).status_code == 404
    assert second_client.get(f"/api/upload-batches/{batch_id}/status/").status_code == 404
    assert second_client.post(f"/api/upload-batches/{batch_id}/items/{item_id}/remove/").status_code == 404
    assert not Document.objects.exists()


def test_oversized_request_is_rejected_before_multipart_parsing_and_marks_failed(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    batch_id, item_id = reserve_one(client)
    response = client.generic(
        "POST",
        upload_path(batch_id, item_id),
        data=b"",
        content_type="multipart/form-data; boundary=boundary",
        CONTENT_LENGTH=str(MAX_MULTIPART_BYTES + 1),
    )
    assert response.status_code == 413
    item = UploadItem.objects.get(pk=item_id)
    assert (item.status, item.error_code) == (UploadItemStatus.UPLOAD_FAILED, "file_too_large")


def test_multiple_files_in_one_item_request_are_rejected(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model)
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: InMemoryObjectStore())
    batch_id, item_id = reserve_one(client)
    response = client.post(
        upload_path(batch_id, item_id),
        {"file": [uploaded_png("one.png"), uploaded_png("two.png", "#d97706")]},
    )
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_upload_request"}}
    assert not Document.objects.exists()


def test_storage_failure_is_503_safe_and_retryable_without_document(django_user_model, monkeypatch):
    client, _patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    store.fail_put = True
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    batch_id, item_id = reserve_one(client, name="must-not-leak.png")

    response = client.post(upload_path(batch_id, item_id), {"file": uploaded_png("must-not-leak.png")})

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "storage_unavailable"}}
    assert "must-not-leak" not in response.content.decode()
    assert UploadItem.objects.get(pk=item_id).status == UploadItemStatus.UPLOAD_FAILED
    assert not Document.objects.exists()


def test_pending_or_failed_item_can_be_removed_but_uploading_cannot(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    batch_id, item_id = reserve_one(client)
    removed = client.post(f"/api/upload-batches/{batch_id}/items/{item_id}/remove/")
    assert removed.status_code == 200
    assert removed.json() == {"removed": True, "batch_deleted": True}
    assert not UploadBatch.objects.filter(pk=batch_id).exists()

    batch_id, item_id = reserve_one(client)
    UploadItem.objects.filter(pk=item_id).update(status=UploadItemStatus.UPLOADING)
    conflict = client.post(f"/api/upload-batches/{batch_id}/items/{item_id}/remove/")
    assert conflict.status_code == 409
    assert UploadItem.objects.filter(pk=item_id).exists()


def test_status_projection_uses_etag_and_contains_no_filename_hash_or_key(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    batch_id, item_id = reserve_one(client, name="secret-diagnosis.png")

    first = client.get(f"/api/upload-batches/{batch_id}/status/")
    etag = first["ETag"]
    text = first.content.decode()
    assert first.status_code == 200
    assert first.json()["counts"] == {"processing": 1, "completed": 0, "failed": 0, "total": 1}
    assert "secret-diagnosis" not in text
    assert "sha256" not in text
    assert "originals/" not in text

    unchanged = client.get(f"/api/upload-batches/{batch_id}/status/", HTTP_IF_NONE_MATCH=etag)
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    UploadItem.objects.filter(pk=item_id).update(status=UploadItemStatus.UPLOAD_FAILED, error_code="unreadable_file")
    changed = client.get(f"/api/upload-batches/{batch_id}/status/", HTTP_IF_NONE_MATCH=etag)
    assert changed.status_code == 200
    assert changed["ETag"] != etag
    assert changed.json()["terminal"] is True


@override_settings(DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE=1, DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE=100)
def test_patient_upload_rate_limit_is_429_with_retry_after(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    assert post_batch(client, [{"name": "one.png", "byte_size": 10}]).status_code == 201
    limited = post_batch(client, [{"name": "two.png", "byte_size": 10}])
    assert limited.status_code == 429
    assert limited.json() == {"error": {"code": "upload_rate_limited"}}
    assert limited["Retry-After"] == "60"


def test_batch_request_rejects_wrong_content_type_and_malformed_json(django_user_model):
    client, _patient = authenticated_client(django_user_model)
    wrong_type = client.post("/api/upload-batches/", {"files": []})
    malformed = client.post("/api/upload-batches/", data="{", content_type="application/json")
    assert wrong_type.status_code == 415
    assert malformed.status_code == 400
    assert not UploadBatch.objects.exists()
