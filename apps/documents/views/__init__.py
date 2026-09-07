"""Stable URL-handler exports for the document views package."""

from .uploads import (
    MAX_MULTIPART_BYTES,
    upload_page,
    create_batch,
    upload_item_content,
    remove_upload_item,
    batch_status,
)
from .records import (
    record_list,
    document_summary,
    document_material,
    document_feedback,
    document_reprocess,
    document_delete,
    trend_index,
    indicator_trend,
)
from .originals import (
    document_viewer,
    document_page_image,
    document_thumbnail_sheet,
    document_original,
)


__all__ = [
    "MAX_MULTIPART_BYTES",
    "upload_page",
    "create_batch",
    "upload_item_content",
    "remove_upload_item",
    "batch_status",
    "record_list",
    "document_summary",
    "document_material",
    "document_feedback",
    "document_reprocess",
    "document_delete",
    "trend_index",
    "indicator_trend",
    "document_viewer",
    "document_page_image",
    "document_thumbnail_sheet",
    "document_original",
]
