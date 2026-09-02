import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.documents.selectors import home_task_cards, task_status_cards
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentDeletionJob,
    DocumentStatus,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "tasks-test"}


def _patient(django_user_model, marker):
    account = django_user_model.objects.create(phone_hash=marker * 64, phone_encrypted="ciphertext")
    patient = create_patient_space(account, "task user", CONFIRMATIONS, EVIDENCE)
    return account, patient


def _document(patient, batch, *, name, status=DocumentStatus.ORGANIZED):
    marker = uuid.uuid4().hex
    return Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename=name,
        content_type="application/pdf",
        byte_size=128,
        page_count=1,
        sha256=marker * 2,
        original_object_key=f"originals/{marker}",
        status=status,
    )


@pytest.mark.django_db
def test_tasks_page_is_server_rendered_complete_patient_scoped_and_not_home_redirect(client, django_user_model):
    account, owner = _patient(django_user_model, "t")
    _foreign_account, foreign = _patient(django_user_model, "u")
    now = timezone.now()

    old_batch = UploadBatch.objects.create(
        patient=owner,
        status=BatchStatus.COMPLETED,
        completed_at=now - timedelta(days=30),
        file_count=1,
        page_count=1,
        byte_size=128,
    )
    old_document = _document(owner, old_batch, name="old-organized.pdf")
    UploadItem.objects.create(
        batch=old_batch,
        ordinal=1,
        display_filename=old_document.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=old_document,
    )

    active_batch = UploadBatch.objects.create(patient=owner, file_count=1)
    UploadItem.objects.create(
        batch=active_batch,
        ordinal=1,
        display_filename="retry-upload.pdf",
        status=UploadItemStatus.UPLOAD_FAILED,
        error_code="unreadable_file",
    )

    foreign_batch = UploadBatch.objects.create(patient=foreign, file_count=1)
    foreign_document = _document(foreign, foreign_batch, name="foreign-secret.pdf")
    UploadItem.objects.create(
        batch=foreign_batch,
        ordinal=1,
        display_filename=foreign_document.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=foreign_document,
    )

    client.force_login(account)
    response = client.get("/tasks/")
    content = response.content.decode()

    assert response.status_code == 200
    assert response.wsgi_request.path == "/tasks/"
    assert 'class="tasks-page home-page"' in content
    assert 'aria-current="page"' not in content
    assert content.count('data-task-card') == 2
    assert "old-organized.pdf" in content
    assert "retry-upload.pdf" in content
    assert "foreign-secret.pdf" not in content
    assert str(foreign_document.pk) not in content
    assert f'href="/records/{old_document.pk}/"' in content
    assert "status-badge--organized" in content
    assert "status-badge--failed" in content
    assert "data-no-tasks" not in content


@pytest.mark.django_db
def test_tasks_page_preserves_explicit_empty_state_for_patient_without_batches(client, django_user_model):
    account, _owner = _patient(django_user_model, "v")
    client.force_login(account)

    response = client.get("/tasks/")
    content = response.content.decode()

    assert response.status_code == 200
    assert 'class="tasks-page home-page"' in content
    assert 'data-no-tasks' in content
    assert content.count('data-task-card') == 0
    assert "\u6682\u65e0\u5904\u7406\u4efb\u52a1\u3002\u4e0a\u4f20\u8d44\u6599\u540e\u53ef\u5728\u8fd9\u91cc\u67e5\u770b\u8fdb\u5ea6\u3002" in content


@pytest.mark.django_db
def test_tasks_page_requires_authenticated_patient(client):
    response = client.get("/tasks/")

    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


@pytest.mark.django_db
def test_task_selectors_keep_two_query_budget_with_empty_batches(django_user_model, django_assert_num_queries):
    _account, owner = _patient(django_user_model, "q")
    UploadBatch.objects.bulk_create([UploadBatch(patient=owner) for _ in range(3)])
    deleting_batch = UploadBatch.objects.create(patient=owner)
    deleting_document = _document(owner, deleting_batch, name="deleting.pdf")
    deleting_document.deleted_at = timezone.now()
    deleting_document.save(update_fields=["deleted_at"])
    DocumentDeletionJob.objects.create(document=deleting_document, object_key=deleting_document.original_object_key)

    with django_assert_num_queries(2):
        home_cards = home_task_cards(owner)

    with django_assert_num_queries(2):
        task_cards = task_status_cards(owner)

    assert deleting_batch.pk not in {card.batch_id for card in home_cards}
    assert deleting_batch.pk not in {card.batch_id for card in task_cards}


@pytest.mark.django_db
def test_tasks_page_exposes_open_original_for_saved_document_item_statuses(client, django_user_model):
    account, owner = _patient(django_user_model, "o")
    batch = UploadBatch.objects.create(patient=owner, file_count=4)
    organized = _document(owner, batch, name="organized.pdf", status=DocumentStatus.ORGANIZED)
    original_only = _document(owner, batch, name="original-only.pdf", status=DocumentStatus.ORIGINAL_ONLY)
    duplicate = _document(owner, batch, name="duplicate.pdf", status=DocumentStatus.ORGANIZED)
    failed = _document(owner, batch, name="failed.pdf", status=DocumentStatus.PROCESSING_FAILED)
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename=organized.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=organized,
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=2,
        display_filename=original_only.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=original_only,
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=3,
        display_filename=duplicate.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.EXACT_DUPLICATE,
        document=duplicate,
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=4,
        display_filename=failed.display_filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=failed,
    )
    client.force_login(account)

    content = client.get("/tasks/").content.decode()
    opened_original = "\u6253\u5f00\u539f\u4ef6"
    for document in (organized, original_only, duplicate):
        assert (
            f'<a class="home-task-item-original" data-task-item-original '
            f'href="/records/{document.pk}/">{opened_original}</a>'
        ) in content
    assert (
        f'<a class="home-task-item-action" data-task-item-action '
        f'href="/records/{failed.pk}/">打开详情并重新整理</a>'
    ) in content
