from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from .batches import item_projection_status, summarize_batch
from .models import (
    BatchStatus,
    Document,
    DocumentStatus,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.processing.models import DatePrecision, ParsingVersion


@dataclass(frozen=True)
class HomeTaskItem:
    item_id: object
    display_filename: str
    status: str
    status_label: str
    status_key: str
    detail_url: str


@dataclass(frozen=True)
class HomeTaskCard:
    batch_id: object
    created_at: object
    page_count: int
    processing: int
    completed: int
    failed: int
    total: int
    main_status: str
    main_status_key: str
    terminal: bool
    items: tuple[HomeTaskItem, ...]


@dataclass(frozen=True)
class HomeRecentDocument:
    pk: object
    created_at: object
    display_filename: str
    content_type: str
    page_count: int
    status: str
    date_label: str
    date_value: str
    institution: str
    file_summary: str
    status_key: str
    status_label: str
    detail_url: str


def recent_documents(patient, *, limit=5):
    safe_limit = max(1, min(int(limit), 5))
    version_query = ParsingVersion.objects.filter(active=True).select_related("document_summary")
    documents = (
        Document.objects.filter(patient=patient, deleted_at__isnull=True)
        .prefetch_related(Prefetch("parsing_versions", queryset=version_query, to_attr="home_active_versions"))
        .order_by("-created_at", "-pk")[:safe_limit]
    )
    return tuple(_recent_document_projection(document) for document in documents)


def _recent_document_projection(document):
    version = document.home_active_versions[0] if document.home_active_versions else None
    summary = getattr(version, "document_summary", None) if version is not None else None
    report_date = summary.document_date if summary is not None else None
    if report_date is not None:
        precision = summary.date_precision
        if precision == DatePrecision.YEAR:
            date_label = f"{report_date.year}年"
            date_value = f"{report_date.year:04d}"
        elif precision == DatePrecision.MONTH:
            date_label = f"{report_date.year}年{report_date.month}月"
            date_value = f"{report_date.year:04d}-{report_date.month:02d}"
        else:
            date_label = f"{report_date.year}年{report_date.month}月{report_date.day}日"
            date_value = report_date.isoformat()
    else:
        uploaded = timezone.localtime(document.created_at)
        date_label = f"{uploaded.year}年{uploaded.month}月{uploaded.day}日上传"
        date_value = uploaded.date().isoformat()
    institution = (summary.institution_raw or "").strip() if summary is not None else ""
    institution = institution or "医疗机构未识别"
    if summary is not None:
        file_type = summary.get_document_type_display()
    elif document.content_type == "application/pdf":
        file_type = "PDF"
    else:
        file_type = "图片"
    return HomeRecentDocument(
        pk=document.pk,
        created_at=document.created_at,
        display_filename=document.display_filename,
        content_type=document.content_type,
        page_count=document.page_count,
        status=document.status,
        date_label=date_label,
        date_value=date_value,
        institution=institution,
        file_summary=f"{file_type} · {document.page_count} 页",
        status_key=_document_status_key(document.status),
        status_label=document.get_status_display(),
        detail_url=reverse("documents:document_summary", args=(document.pk,)),
    )


def _document_status_key(value):
    return {
        DocumentStatus.PROCESSING: "processing",
        DocumentStatus.ORGANIZED: "organized",
        DocumentStatus.ORIGINAL_ONLY: "original",
        DocumentStatus.PROCESSING_FAILED: "failed",
    }.get(value, "processing")


def _status_label(value):
    if value in DocumentStatus.values:
        return DocumentStatus(value).label
    return UploadItemStatus(value).label


def _main_status(counts):
    if counts.processing:
        return "处理中"
    if counts.failed and counts.completed:
        return "部分完成"
    if counts.failed:
        return "处理失败"
    if counts.completed:
        return "已完成"
    return "待上传"


def _main_status_key(counts):
    if counts.processing:
        return "processing"
    if counts.failed:
        return "failed"
    if counts.completed:
        return "organized"
    return "processing"


def _item_status_key(value):
    if value in {UploadItemStatus.PENDING, UploadItemStatus.UPLOADING, DocumentStatus.PROCESSING}:
        return "processing"
    if value == UploadItemStatus.EXACT_DUPLICATE:
        return "saved"
    if value == DocumentStatus.ORGANIZED:
        return "organized"
    if value == DocumentStatus.ORIGINAL_ONLY:
        return "original"
    return "failed"


def _task_item_projection(item):
    status = str(item_projection_status(item))
    document = getattr(item, "document", None)
    detail_url = ""
    if document is not None:
        detail_url = reverse("documents:document_summary", args=(document.pk,))
    return HomeTaskItem(
        item_id=item.pk,
        display_filename=item.display_filename,
        status=status,
        status_label=_status_label(status),
        status_key=_item_status_key(status),
        detail_url=detail_url,
    )


def home_task_cards(patient, *, now=None):
    now = timezone.now() if now is None else now
    cutoff = now - timedelta(days=7)
    item_query = UploadItem.objects.select_related("document").order_by("ordinal", "pk")
    batches = (
        UploadBatch.objects.filter(patient=patient)
        .filter(Q(status=BatchStatus.ACTIVE) | Q(status=BatchStatus.COMPLETED, completed_at__gte=cutoff))
        .prefetch_related(Prefetch("items", queryset=item_query, to_attr="home_items"))
        .order_by("-created_at", "-pk")
    )
    cards = []
    for batch in batches:
        items = tuple(batch.home_items)
        if not items and batch.documents.filter(deleted_at__isnull=False, deletion_job__isnull=False).exists():
            continue
        counts = summarize_batch(batch, items=items)
        cards.append(
            HomeTaskCard(
                batch_id=batch.pk,
                created_at=batch.created_at,
                page_count=batch.page_count,
                processing=counts.processing,
                completed=counts.completed,
                failed=counts.failed,
                total=counts.total,
                main_status=_main_status(counts),
                main_status_key=_main_status_key(counts),
                terminal=counts.terminal,
                items=tuple(
                    _task_item_projection(item)
                    for item in items
                ),
            )
        )
    return tuple(cards)
