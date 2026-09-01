from datetime import date, datetime, timedelta
import re
import uuid

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.documents.batches import refresh_batch_state
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentStatus,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.documents.selectors import home_task_cards, recent_documents
from apps.patients.models import Patient
from apps.patients.services import create_patient_space
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    ParsingVersion,
    ParsingVersionStatus,
)


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


def publish_summary(value, *, report_date, institution, document_type=DocumentType.LAB, precision=DatePrecision.DAY):
    run = ProcessingRun.objects.create(
        document=value,
        parser_version="home-presenter-v1",
        task_type="initial",
        idempotency_key=f"{value.pk}:home-presenter-v1:initial",
        stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(),
        is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=value,
        processing_run=run,
        parser_version="home-presenter-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        status=ParsingVersionStatus.READY,
    )
    ParsingVersion.objects.activate(version)
    return DocumentSummary.objects.create(
        parsing_version=version,
        document_type=document_type,
        document_date_raw=report_date.isoformat(),
        document_date=report_date,
        date_precision=precision,
        institution_raw=institution,
        confidence="0.9000",
    )


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
def test_recent_documents_project_report_metadata_and_safe_upload_fallbacks(django_user_model):
    _account, owner = patient(django_user_model)
    upload_time = timezone.make_aware(datetime(2026, 8, 21, 9, 30))
    parsed = document(
        owner,
        name="检验报告.pdf",
        status=DocumentStatus.ORGANIZED,
        created_at=upload_time - timedelta(days=1),
    )
    publish_summary(
        parsed,
        report_date=date(2026, 8, 20),
        institution="合成检验中心",
    )
    fallback = document(
        owner,
        name="手机照片.png",
        status=DocumentStatus.ORIGINAL_ONLY,
        created_at=upload_time,
    )

    cards = tuple(recent_documents(owner))

    assert all(
        hasattr(card, field)
        for card in cards
        for field in (
            "date_label",
            "date_value",
            "display_filename",
            "institution",
            "file_summary",
            "status_key",
            "status_label",
            "detail_url",
        )
    )
    by_id = {card.pk: card for card in cards}
    assert by_id[parsed.pk].date_label == "2026年8月20日"
    assert by_id[parsed.pk].date_value == "2026-08-20"
    assert by_id[parsed.pk].institution == "合成检验中心"
    assert by_id[parsed.pk].file_summary == "检验报告 · 1 页"
    assert by_id[parsed.pk].status_key == "organized"
    assert by_id[parsed.pk].status_label == "已整理"
    assert by_id[parsed.pk].detail_url == f"/records/{parsed.pk}/"
    assert by_id[fallback.pk].date_label == "2026年8月21日上传"
    assert by_id[fallback.pk].date_value == "2026-08-21"
    assert by_id[fallback.pk].institution == "医疗机构未识别"
    assert by_id[fallback.pk].file_summary == "图片 · 1 页"
    assert by_id[fallback.pk].status_key == "original"
    assert by_id[fallback.pk].status_label == "仅原件"
    assert by_id[fallback.pk].detail_url == f"/records/{fallback.pk}/"


@pytest.mark.django_db
def test_recent_document_projection_uses_a_fixed_query_budget_for_five_summaries(django_user_model):
    _account, owner = patient(django_user_model)
    upload_time = timezone.make_aware(datetime(2026, 8, 21, 9, 30))
    documents = []
    for index in range(5):
        value = document(
            owner,
            name=f"report-{index}.pdf",
            status=DocumentStatus.ORGANIZED,
            created_at=upload_time - timedelta(minutes=index),
        )
        publish_summary(
            value,
            report_date=date(2026, 8, 20 - index),
            institution=f"Synthetic hospital {index}",
        )
        documents.append(value)

    with CaptureQueriesContext(connection) as captured:
        cards = tuple(recent_documents(owner))
        assert [card.pk for card in cards] == [value.pk for value in documents]
        assert [card.date_value for card in cards] == [f"2026-08-{20 - index:02d}" for index in range(5)]

    assert len(captured) <= 2


@pytest.mark.django_db
def test_recent_document_projection_respects_report_date_precision(django_user_model):
    _account, owner = patient(django_user_model)
    month = document(owner, name="month.pdf", status=DocumentStatus.ORGANIZED)
    publish_summary(month, report_date=date(2026, 8, 1), institution="Month clinic", precision=DatePrecision.MONTH)
    year = document(owner, name="year.pdf", status=DocumentStatus.ORGANIZED)
    publish_summary(year, report_date=date(2025, 1, 1), institution="Year clinic", precision=DatePrecision.YEAR)

    cards = {card.pk: card for card in recent_documents(owner)}

    assert cards[month.pk].date_label == "2026年8月"
    assert cards[month.pk].date_value == "2026-08"
    assert cards[year.pk].date_label == "2025年"
    assert cards[year.pk].date_value == "2025"


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
    assert hasattr(card, "main_status_key")
    assert card.main_status_key == "processing"
    assert [item.status_label for item in card.items] == [
        "待上传",
        "上传失败",
        "处理中",
        "已整理",
        "处理失败",
    ]
    assert [item.status_key for item in card.items] == [
        "processing",
        "failed",
        "processing",
        "organized",
        "failed",
    ]
    assert card.items[1].detail_url == ""
    assert card.items[2].detail_url == f"/records/{processing.pk}/"
    assert card.items[4].detail_url == f"/records/{failed.pk}/"
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
def test_home_empty_state_has_primary_and_mobile_upload_actions_without_medical_conclusion(client, django_user_model):
    account, _owner = patient(django_user_model, onboarded=True)
    client.force_login(account)

    response = client.get("/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "把自己和家人的健康资料，安心收在一起" in content
    assert "支持图片和 PDF 批量选择" in content
    assert "无需补录医疗信息" in content
    assert "原件先保存，再自动整理" in content
    assert "这里还没有资料。上传图片或 PDF 后，原件会先安全保存。" in content
    assert content.count('href="/uploads/new/"') == 2
    assert "选择图片或 PDF" in content
    assert "disabled" not in content
    assert "异常提醒" not in content
    assert "诊断结论" not in content
    assert "/static/js/task-status.js" in content


@pytest.mark.django_db
def test_home_task_failures_distinguish_unsaved_uploads_from_saved_originals_and_link_details(
    client,
    django_user_model,
):
    account, owner = patient(django_user_model, onboarded=True)
    batch = UploadBatch.objects.create(patient=owner, file_count=2, page_count=1, byte_size=128)
    failed_document = document(
        owner,
        batch=batch,
        name="saved-original.png",
        status=DocumentStatus.PROCESSING_FAILED,
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename="not-saved.pdf",
        status=UploadItemStatus.UPLOAD_FAILED,
        error_code="unreadable_file",
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=2,
        display_filename="saved-original.png",
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=failed_document,
    )
    client.force_login(account)

    content = client.get("/").content.decode()
    task_start = content.index("data-task-card")
    task_end = content.index("</article>", task_start)
    task_markup = content[task_start:task_end]

    assert "上传失败，原件尚未保存。" in task_markup
    assert "整理未完成，原件已经保存。" in task_markup
    assert f'href="/records/{failed_document.pk}/"' in task_markup
    assert "打开详情并重新整理" in task_markup
    assert content.count('href="/uploads/new/"') == 2


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
    assert "医疗机构未识别" in content
    assert "图片 · 1 页" in content
    assert "打开原件" in content
    assert "status-badge--processing" in content
    assert "查看详情" in content
    assert 'data-task-card' in content
    assert 'data-status-url="/api/upload-batches/' in content
    assert 'data-terminal="false"' in content
    assert f'data-task-item-id="{own_batch.items.get().pk}"' in content
    assert re.search(r'<li[^>]*data-task-item-id="[^"]+"[^>]*>.*?data-task-item-help=', content, re.DOTALL)
    assert re.search(
        rf'<li[^>]*data-task-item-id="{own_batch.items.get().pk}"[^>]*>.*?'
        rf'data-task-item-action[^>]*href="/records/{own_document.pk}/"[^>]*hidden',
        content,
        re.DOTALL,
    )


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
