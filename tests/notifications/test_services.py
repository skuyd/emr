import json
import uuid

from django.db import transaction
from django.utils import timezone
import pytest

from apps.documents.batches import refresh_batch_state
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentStatus,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.notifications.models import NotificationKind, TaskNotification
from apps.notifications.services import (
    create_task_notification,
    serialize_notification,
)
from apps.patients.models import Patient


COMPLETED_TITLE = "资料整理完成"
COMPLETED_BODY = "你上传的资料已整理完成，点击查看结果。"
FAILED_TITLE = "资料整理有未完成项目"
FAILED_BODY = "你上传的资料中有未完成项目，点击查看任务状态。"
FORBIDDEN_MARKERS = (
    "FORBIDDEN_PATIENT_IDENTITY",
    "FORBIDDEN_FILENAME",
    "FORBIDDEN_INDICATOR",
    "987.654",
    "FORBIDDEN_DIAGNOSIS",
)


def make_patient(django_user_model, *, display_name="测试用户"):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    return account, Patient.objects.create(account=account, display_name=display_name)


def make_completed_batch(patient, *, failed=False, filename="synthetic.png"):
    batch = UploadBatch.objects.create(
        patient=patient,
        file_count=1,
        page_count=1,
        byte_size=128,
        status=BatchStatus.COMPLETED,
        completed_at=timezone.now(),
    )
    if failed:
        UploadItem.objects.create(
            batch=batch,
            ordinal=1,
            display_filename=filename,
            byte_size=128,
            page_count=1,
            status=UploadItemStatus.UPLOAD_FAILED,
            error_code="synthetic_failure",
        )
        return batch
    marker = uuid.uuid4().hex
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename=filename,
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=marker * 2,
        original_object_key=f"originals/{marker}",
        status=DocumentStatus.ORGANIZED,
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename=filename,
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=document,
    )
    return batch


@pytest.mark.django_db
def test_finished_batch_creates_one_exact_generic_notification(django_user_model):
    _account, patient = make_patient(
        django_user_model,
        display_name="FORBIDDEN_PATIENT_IDENTITY",
    )
    batch = make_completed_batch(
        patient,
        filename="FORBIDDEN_FILENAME-FORBIDDEN_INDICATOR-987.654-FORBIDDEN_DIAGNOSIS.png",
    )

    first = create_task_notification(batch.pk)
    second = create_task_notification(batch.pk)
    payload = serialize_notification(first)

    assert first.pk == second.pk
    assert TaskNotification.objects.filter(batch=batch).count() == 1
    assert first.kind == NotificationKind.COMPLETED
    assert payload == {
        "notification_id": str(first.pk),
        "title": COMPLETED_TITLE,
        "body": COMPLETED_BODY,
        "created_at": first.created_at.isoformat(),
        "read": False,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert all(marker not in serialized for marker in FORBIDDEN_MARKERS)


@pytest.mark.django_db
def test_failed_batch_uses_generic_failure_variant(django_user_model):
    _account, patient = make_patient(django_user_model)
    batch = make_completed_batch(patient, failed=True)

    notification = create_task_notification(batch.pk)

    assert notification.kind == NotificationKind.FAILED
    assert serialize_notification(notification)["title"] == FAILED_TITLE
    assert serialize_notification(notification)["body"] == FAILED_BODY


@pytest.mark.django_db
def test_active_or_empty_batch_does_not_create_notification(django_user_model):
    _account, patient = make_patient(django_user_model)
    active = UploadBatch.objects.create(patient=patient)

    assert create_task_notification(active.pk) is None
    assert not TaskNotification.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_batch_transition_triggers_notification_once(django_user_model):
    _account, patient = make_patient(django_user_model)
    batch = make_completed_batch(patient)
    UploadBatch.objects.filter(pk=batch.pk).update(status=BatchStatus.ACTIVE, completed_at=None)
    batch.refresh_from_db()

    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        refresh_batch_state(locked)

    assert TaskNotification.objects.filter(batch=batch).count() == 1
    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        refresh_batch_state(locked)
    assert TaskNotification.objects.filter(batch=batch).count() == 1

