from datetime import timedelta
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
from apps.documents.selectors import home_task_cards, recent_documents
from apps.patients.models import Patient
from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "home-test"}


def patient(django_user_model, *, onboarded=False):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    if onboarded:
        owner = create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    else:
        owner = Patient.objects.create(account=account, display_name="测试患者")
    return account, owner


def document(owner, *, batch=None, name=None, status=DocumentStatus.PROCESSING, created_at=None, deleted_at=None):
    batch = batch or UploadBatch.objects.create(patient=owner)
    marker = uuid.uuid4().hex
    value = Document.objects.create(
        patient=owner,
        batch=batch,
        display_filename=name or f"synthetic-{marker}.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=marker * 2,
        original_object_key=f"originals/{marker}",
        status=status,
        deleted_at=deleted_at,
    )
    if created_at is not None:
        Document.objects.filter(pk=value.pk).update(created_at=created_at)
        value.created_at = created_at
    return value


@pytest.mark.django_db
def test_recent_documents_are_active_patient_scoped_newest_five_without_status_priority(django_user_model):
    _account, owner = patient(django_user_model)
    _foreign_account, foreign = patient(django_user_model)
    now = timezone.now()
    created = []
    statuses = [
        DocumentStatus.PROCESSING_FAILED,
        DocumentStatus.ORIGINAL_ONLY,
        DocumentStatus.ORGANIZED,
        DocumentStatus.PROCESSING,
        DocumentStatus.ORGANIZED,
        DocumentStatus.PROCESSING_FAILED,
    ]
    for index, status in enumerate(statuses):
        created.append(document(owner, status=status, created_at=now - timedelta(minutes=index)))
    deleted = document(owner, created_at=now + timedelta(minutes=1), deleted_at=now)
    foreign_document = document(foreign, created_at=now + timedelta(minutes=2), name="foreign-secret.png")

    selected = list(recent_documents(owner))

    assert [item.pk for item in selected] == [item.pk for item in created[:5]]
    assert selected[0].status == DocumentStatus.PROCESSING_FAILED
    assert deleted.pk not in {item.pk for item in selected}
    assert foreign_document.pk not in {item.pk for item in selected}


@pytest.mark.django_db
def test_task_cards_include_all_active_and_completed_at_exact_seven_day_boundary(django_user_model):
    _account, owner = patient(django_user_model)
    now = timezone.now()
    cutoff = now - timedelta(days=7)
    old_active = UploadBatch.objects.create(patient=owner)
    exact = UploadBatch.objects.create(patient=owner)
    exact.status = BatchStatus.COMPLETED
    exact.completed_at = cutoff
    exact.save(update_fields=["status", "completed_at"])
    expired = UploadBatch.objects.create(patient=owner)
    expired.status = BatchStatus.COMPLETED
    expired.completed_at = cutoff - timedelta(microseconds=1)
    expired.save(update_fields=["status", "completed_at"])

    cards = home_task_cards(owner, now=now)

    assert {card.batch_id for card in cards} == {old_active.pk, exact.pk}
    assert expired.pk not in {card.batch_id for card in cards}


@pytest.mark.django_db
def test_task_card_counts_share_authoritative_projection_and_do_not_mutate_batch(django_user_model):
    _account, owner = patient(django_user_model)
    batch = UploadBatch.objects.create(patient=owner, file_count=5, page_count=3, byte_size=384)
    processing = document(owner, batch=batch, status=DocumentStatus.PROCESSING, name="processing.png")
    organized = document(owner, batch=batch, status=DocumentStatus.ORGANIZED, name="organized.png")
    failed = document(owner, batch=batch, status=DocumentStatus.PROCESSING_FAILED, name="failed.png")
    UploadItem.objects.bulk_create(
        [
            UploadItem(batch=batch, ordinal=1, display_filename="pending.png"),
            UploadItem(
                batch=batch,
                ordinal=2,
                display_filename="upload-failed.png",
                status=UploadItemStatus.UPLOAD_FAILED,
                error_code="unreadable_file",
            ),
            UploadItem(
                batch=batch,
                ordinal=3,
                display_filename="processing.png",
                byte_size=128,
                page_count=1,
                status=UploadItemStatus.CREATED,
                document=processing,
            ),
            UploadItem(
                batch=batch,
                ordinal=4,
                display_filename="organized.png",
                byte_size=128,
                page_count=1,
                status=UploadItemStatus.CREATED,
                document=organized,
            ),
            UploadItem(
                batch=batch,
                ordinal=5,
                display_filename="failed.png",
                byte_size=128,
                page_count=1,
                status=UploadItemStatus.CREATED,
                document=failed,
            ),
        ]
    )

    card = home_task_cards(owner)[0]

    assert (card.processing, card.completed, card.failed, card.total) == (2, 1, 2, 5)
    assert card.main_status == "处理中"
    assert [item.status_label for item in card.items] == [
        "待上传",
        "上传失败",
        "处理中",
        "已整理",
        "处理失败",
    ]
    batch.refresh_from_db()
    assert batch.status == BatchStatus.ACTIVE


@pytest.mark.django_db
def test_task_cards_are_newest_first_and_use_two_queries_without_per_item_n_plus_one(
    django_user_model, django_assert_num_queries
):
    _account, owner = patient(django_user_model)
    older = UploadBatch.objects.create(patient=owner)
    newer = UploadBatch.objects.create(patient=owner)
    now = timezone.now()
    UploadBatch.objects.filter(pk=older.pk).update(created_at=now - timedelta(seconds=1))
    UploadBatch.objects.filter(pk=newer.pk).update(created_at=now)
    UploadItem.objects.create(batch=older, ordinal=1, display_filename="older.png")
    UploadItem.objects.create(batch=newer, ordinal=1, display_filename="newer.png")

    with django_assert_num_queries(2):
        cards = home_task_cards(owner)
        assert [card.batch_id for card in cards] == [newer.pk, older.pk]


@pytest.mark.django_db
def test_home_empty_state_has_one_working_primary_upload_action_and_no_medical_conclusion(client, django_user_model):
    account, _owner = patient(django_user_model, onboarded=True)
    client.force_login(account)

    response = client.get("/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "还没有资料。上传检查单、报告图片或 PDF，系统会自动帮你整理。" in content
    assert content.count('href="/uploads/new/"') == 1
    assert "上传资料" in content
    assert "disabled" not in content
    assert "异常提醒" not in content
    assert "诊断结论" not in content
    assert "/static/js/task-status.js" in content


@pytest.mark.django_db
def test_home_renders_only_current_patients_recent_files_and_expandable_task_details(client, django_user_model):
    account, owner = patient(django_user_model, onboarded=True)
    _foreign_account, foreign = patient(django_user_model)
    own_batch = UploadBatch.objects.create(patient=owner, file_count=1, page_count=1, byte_size=128)
    own_document = document(owner, batch=own_batch, name="own-visible.png")
    UploadItem.objects.create(
        batch=own_batch,
        ordinal=1,
        display_filename="own-visible.png",
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=own_document,
    )
    foreign_document = document(foreign, name="foreign-secret.png")
    client.force_login(account)

    content = client.get("/").content.decode()

    assert "own-visible.png" in content
    assert "foreign-secret.png" not in content
    assert str(foreign_document.pk) not in content
    assert f'/records/{own_document.pk}/' in content
    assert "查看详情" in content
    assert 'data-task-card' in content
    assert 'data-status-url="/api/upload-batches/' in content
    assert "日期未识别" in content


@pytest.mark.django_db
def test_pending_document_summary_is_patient_scoped_and_excludes_soft_deleted(client, django_user_model):
    account, owner = patient(django_user_model, onboarded=True)
    _foreign_account, foreign = patient(django_user_model)
    visible = document(owner, name="own-visible.png")
    hidden = document(owner, name="deleted-secret.png", deleted_at=timezone.now())
    foreign_document = document(foreign, name="foreign-secret.png")
    client.force_login(account)

    response = client.get(f"/records/{visible.pk}/")

    assert response.status_code == 200
    assert "own-visible.png" in response.content.decode()
    assert client.get(f"/records/{hidden.pk}/").status_code == 404
    assert client.get(f"/records/{foreign_document.pk}/").status_code == 404


@pytest.mark.django_db
def test_final_item_transition_completes_batch_for_home_retention(django_user_model):
    _account, owner = patient(django_user_model)
    batch = UploadBatch.objects.create(patient=owner, file_count=1)
    item = UploadItem.objects.create(batch=batch, ordinal=1, display_filename="failed.png")
    item.status = UploadItemStatus.UPLOAD_FAILED
    item.error_code = "unreadable_file"
    item.save(update_fields=["status", "error_code"])

    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        counts = refresh_batch_state(locked)

    assert counts.terminal is True
    assert home_task_cards(owner)[0].main_status == "处理失败"
