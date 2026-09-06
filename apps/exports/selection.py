"""Document date intervals are kept at their recorded precision."""

from calendar import monthrange
from datetime import date
from uuid import UUID

from django.core.exceptions import PermissionDenied

from apps.documents.models import Document
from apps.labs.readmodels import effective_document_date, effective_rows
from apps.patients.models import Patient

from .errors import ExportInputError


def identifiers(values):
    # This also validates server-generated selections (all owned documents and
    # their facts/labs); an unrelated form-field limit must not cap that scope.
    if not isinstance(values, (list, tuple, set)):
        raise ExportInputError("资料选择无效。")
    try:
        return sorted({str(UUID(str(value))) for value in values})
    except (ValueError, TypeError, AttributeError):
        raise ExportInputError("资料标识无效。") from None


def document_record(document, rows=()):
    version = document.parsing_versions.filter(active=True).select_related("document_summary").first()
    summary = getattr(version, "document_summary", None) if version else None
    document_date, precision = effective_document_date(
        rows, summary.document_date if summary else None, summary.date_precision if summary else "UNKNOWN",
    )
    # Corrected laboratory dates are explicit evidence; do not retain an obsolete date label.
    corrected = any(getattr(row, "date_verified", False) for row in rows)
    return {
        "id": str(document.pk), "filename": document.display_filename, "content_type": document.content_type,
        "byte_size": document.byte_size, "page_count": document.page_count, "sha256": document.sha256,
        "status": document.status, "lifecycle_revision": document.lifecycle_revision,
        "parsing_version": str(version.pk) if version else None,
        "document_type": summary.document_type if summary else "UNKNOWN",
        "date": document_date.isoformat()[:4] if document_date and precision == "YEAR"
        else document_date.isoformat()[:7] if document_date and precision == "MONTH"
        else document_date.isoformat() if document_date else None,
        "date_precision": precision, "date_raw": document_date.isoformat() if corrected and document_date
        else "" if corrected else summary.document_date_raw if summary else "",
        "institution": summary.institution_raw if summary else "",
    }


def _interval(item):
    value, precision = item["date"], item["date_precision"]
    if not value or precision == "UNKNOWN":
        return None
    try:
        if precision == "YEAR":
            return date(int(value[:4]), 1, 1), date(int(value[:4]), 12, 31)
        if precision == "MONTH":
            year, month = map(int, value[:7].split("-"))
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        day = date.fromisoformat(value)
        return day, day
    except (TypeError, ValueError):
        return None


def select_documents(patient, selection, *, rows=None):
    if not Patient.objects.filter(pk=patient.pk, account__is_active=True).exists():
        raise PermissionDenied
    mode = selection.get("mode", "all")
    if mode not in {"all", "documents", "dates"}:
        raise ExportInputError("请选择资料、日期范围或全部资料。")
    chosen = identifiers(selection.get("document_ids", []))
    unknown_ids = identifiers(selection.get("unknown_ids", []))
    submitted = set(chosen) | set(unknown_ids)
    owned = {str(pk) for pk in Document.objects.filter(patient=patient, pk__in=submitted).values_list("pk", flat=True)}
    if submitted - owned:
        raise PermissionDenied
    rows = effective_rows(patient, include_uncertain=True) if rows is None else rows
    items = [document_record(document, [row for row in rows if row.parsing_version.document_id == document.pk])
             for document in Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("pk")]
    if submitted - {item["id"] for item in items}:
        raise ExportInputError("部分选定资料已不可用，请重新选择。")
    start = end = None
    if mode == "dates":
        try:
            start, end = (date.fromisoformat(selection.get(key, "")) for key in ("start", "end"))
        except (TypeError, ValueError):
            raise ExportInputError("请输入完整且有效的起止日期。") from None
        if start > end:
            raise ExportInputError("开始日期不能晚于结束日期。")
    selected, excluded, uncertain = [], [], []
    eligible_unknown = set()
    for item in items:
        include, reason = mode == "all" or (mode == "documents" and item["id"] in chosen), "未选择此资料"
        if mode == "dates":
            interval = _interval(item)
            ambiguous = interval is None or (
                interval[0] <= end and interval[1] >= start and not (start <= interval[0] and interval[1] <= end)
            )
            if ambiguous:
                eligible_unknown.add(item["id"])
                uncertain.append({**item, "reason": "日期未知或精度不足，需明确选择"})
                include, reason = item["id"] in unknown_ids, "日期未知或精度不足，尚未选择纳入"
            else:
                include = start <= interval[0] and interval[1] <= end
                reason = "资料日期不在范围内"
        (selected if include else excluded).append(item if include else {**item, "reason": reason})
    if mode == "dates" and set(unknown_ids) - eligible_unknown:
        raise ExportInputError("日期不确定的选择已变化，请重新确认筛选清单。")
    return {"documents": selected, "excluded": excluded, "uncertain": uncertain}
