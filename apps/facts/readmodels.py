"""One admission rule shared by review, cards and structured exports."""

from copy import deepcopy
import hashlib
import json

from django.db.models import Q
from django.urls import reverse

from apps.processing.models import OcrBlock, ParsingVersion

from .models import Fact, FactCategory


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def fact_queryset():
    return Fact.objects.select_related(
        "document__patient__account", "document_page", "parsing_version__document_summary", "evidence",
    ).order_by("document__created_at", "document_page__page_number", "reading_order", "pk")


def source_token(fact):
    values = {"document": str(fact.document_id), "sha256": fact.document.sha256,
              "page": str(fact.document_page_id), "raw_text": fact.raw_text, "content": fact.automatic_content}
    if fact.origin == "MANUAL":
        version = fact.document.parsing_versions.filter(active=True).first()
        values["page_content"] = list(OcrBlock.objects.filter(
            parsing_version=version, document_page_id=fact.document_page_id,
        ).order_by("reading_order", "pk").values_list("text", "polygon")) if version else []
    else:
        values["evidence"] = (fact.evidence.source_text, fact.evidence.polygon) if fact.evidence_id else None
        summary = getattr(fact.parsing_version, "document_summary", None)
        values["metadata"] = [summary.document_date_raw, summary.document_date, summary.date_precision, summary.institution_raw] if summary else None
    return digest(values)


def source_info(fact):
    version = fact.document.parsing_versions.filter(active=True).first() if fact.origin == "MANUAL" else fact.parsing_version
    current_region = fact.evidence.polygon if fact.evidence_id else None
    path = reverse("documents:document_viewer", args=[fact.document_id]) + f"?page={fact.document_page.page_number}"
    if fact.evidence_id and version and version.active:
        path += f"&evidence={fact.evidence_id}"
    return {
        "document_id": str(fact.document_id), "filename": fact.document.display_filename,
        "page": fact.document_page.page_number, "page_id": str(fact.document_page_id),
        "parsing_version": str(version.pk) if version else None,
        "evidence_id": str(fact.evidence_id) if fact.evidence_id else None,
        "raw_text": fact.raw_text, "polygon": current_region, "location": "REGION" if current_region else "PAGE",
        "url": path, "sha256": fact.document.sha256,
    }


def _revision(fact, visited=None):
    own = fact.revisions.order_by("-sequence").first()
    if own or fact.origin == "MANUAL":
        return own, []
    visited = set() if visited is None else visited
    if fact.pk in visited:
        return None, []
    visited.add(fact.pk)
    previous = fact.parsing_version.previous_version_id
    if previous is None or not ParsingVersion.objects.filter(pk=previous, document_id=fact.document_id).exists():
        return None, []
    token = source_token(fact)
    current = [row for row in fact_queryset().filter(parsing_version=fact.parsing_version, category=fact.category) if source_token(row) == token]
    earlier = [row for row in fact_queryset().filter(parsing_version_id=previous, category=fact.category) if source_token(row) == token]
    if len(current) != 1 or len(earlier) != 1:
        return None, []
    revision, ancestors = _revision(earlier[0], visited)
    return revision, [str(earlier[0].pk), *ancestors]


def effective_fact(fact):
    token = source_token(fact)
    revision, ancestors = _revision(fact)
    state = deepcopy(revision.after) if revision else {
        "content": deepcopy(fact.automatic_content), "status": "PENDING", "source_token": token,
    }
    historical = fact.origin == "AUTOMATIC" and not fact.parsing_version.active
    invalid_source = (
        fact.document.deleted_at is not None or not fact.document.patient.account.is_active
        or fact.document_page.document_id != fact.document_id
        or (fact.origin == "AUTOMATIC" and (
            not fact.evidence_id or fact.evidence.parsing_version_id != fact.parsing_version_id
            or fact.evidence.document_page_id != fact.document_page_id
            or fact.evidence.source_text != fact.raw_text
        ))
    )
    changed = state.get("source_token") != token
    if changed or historical or invalid_source:
        state["status"] = "PENDING"
    reason = (
        "来源不可用" if invalid_source else "旧解析的人工记录，请对照当前原件重新补录核对" if historical
        else "来源内容已变化，请重新核对" if changed
        else {"PENDING": "尚未核对", "DEFERRED": "暂不处理", "EXCLUDED": "已排除"}.get(state["status"], "")
    )
    return {
        **state, "id": str(fact.pk), "origin": fact.origin, "category": state["content"]["category"],
        "category_label": FactCategory(state["content"]["category"]).label, "source": source_info(fact),
        "revision_number": fact.revision_number, "revision_id": str(revision.pk) if revision else None,
        "inherited_from": ancestors, "historical": historical,
        "source_valid": not (historical or invalid_source),
        "usable": state["status"] == "CONFIRMED" and not (historical or invalid_source or changed),
        "reason": reason, "current_source_token": token,
    }


def review_facts(patient, *, document=None, include_history=False):
    query = fact_queryset().filter(document__patient=patient, document__deleted_at__isnull=True)
    if document is not None:
        query = query.filter(document=document)
    current = [effective_fact(row) for row in query.filter(Q(parsing_version__active=True) | Q(origin="MANUAL"))]
    if include_history:
        represented = {row["id"] for row in current}
        represented.update(item for row in current for item in row["inherited_from"])
        current.extend(effective_fact(row) for row in query.filter(
            origin="AUTOMATIC", parsing_version__active=False, revision_number__gt=0,
        ) if str(row.pk) not in represented)
    for row in current:
        row["report_differences"] = any(
            other["category"] == row["category"] and other["source"]["document_id"] != row["source"]["document_id"]
            and other["content"]["text"] != row["content"]["text"] and not other["historical"]
            for other in current
        )
    return tuple(current)


def usable_facts(patient, *, document_ids=None):
    rows = review_facts(patient)
    selected = {str(value) for value in document_ids} if document_ids is not None else None
    return tuple(row for row in rows if row["usable"] and (selected is None or row["source"]["document_id"] in selected))
