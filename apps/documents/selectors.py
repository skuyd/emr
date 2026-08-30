from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Prefetch, Q
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


@dataclass(frozen=True)
class HomeTaskItem:
    item_id: object
    display_filename: str
    status: str
    status_label: str


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
    terminal: bool
    items: tuple[HomeTaskItem, ...]


def recent_documents(patient, *, limit=5):
    safe_limit = max(1, min(int(limit), 5))
    return Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("-created_at", "-pk")[:safe_limit]


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
                terminal=counts.terminal,
                items=tuple(
                    HomeTaskItem(
                        item_id=item.pk,
                        display_filename=item.display_filename,
                        status=str(item_projection_status(item)),
                        status_label=_status_label(item_projection_status(item)),
                    )
                    for item in items
                ),
            )
        )
    return tuple(cards)
