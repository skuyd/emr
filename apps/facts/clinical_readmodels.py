"""Effective report fields; conflicting confirmed values are never overwritten."""

from copy import deepcopy
from django.core.exceptions import ValidationError

from apps.patients.access import authorize_patient

from .clinical_schema import FIELDS
from .models import ClinicalReport, Fact
from .readmodels import digest


def report_queryset():
    return ClinicalReport.objects.select_related("document__patient__account", "parsing_version", "created_by").prefetch_related(
        "spans__ocr_block", "spans__document_page", "revisions",
    )


def report_source_token(report):
    active = report.document.parsing_versions.filter(active=True).values("pk", "published_at", "updated_at").first()
    return digest({
        "report_id": str(report.pk), "version": str(report.parsing_version_id),
        "active_version": {key: str(value) for key, value in active.items()} if active else None,
        "document_sha256": report.document.sha256, "lifecycle_revision": report.document.lifecycle_revision,
        "schema": report.schema_version, "segmenter": report.segmenter_version,
        "boundary_fingerprint": report.source_fingerprint,
        "spans": [(span.ordinal, str(span.document_page_id), str(span.ocr_block_id), span.start_offset, span.end_offset,
                   span.raw_text, span.ocr_block.text if span.ocr_block_id else None,
                   span.ocr_block.polygon if span.ocr_block_id else None) for span in report.spans.all()],
    })


def report_state(report):
    latest = report.revisions.order_by("-sequence").first()
    status = latest.after["status"] if latest else "ACTIVE"
    active = report.document.parsing_versions.filter(active=True).values_list("pk", flat=True).first()
    historical = report.parsing_version_id != active
    unavailable = bool(report.document.deleted_at or report.document.patient.deleted_at or not report.document.patient.account.is_active)
    damaged = False
    spans = list(report.spans.all())
    for span in spans:
        if span.document_page.document_id != report.document_id:
            damaged = True
        if span.ocr_block_id and (
            span.ocr_block.parsing_version_id != report.parsing_version_id
            or span.raw_text != span.ocr_block.text[span.start_offset:span.end_offset]
        ):
            damaged = True
    source_valid = not (historical or unavailable or damaged or not spans)
    return {"id": str(report.pk), "document_id": str(report.document_id), "parsing_version": str(report.parsing_version_id) if report.parsing_version_id else None,
            "origin": report.origin, "title": report.title, "routing_kind": report.routing_kind,
            "ordinal": report.ordinal, "schema_version": report.schema_version, "segmenter_version": report.segmenter_version,
            "boundary_state": report.boundary_state, "limitations": list(report.limitations),
            "status": status, "status_label": "已排除" if status == "EXCLUDED" else "当前报告",
            "source_valid": source_valid, "historical": historical,
            "revision_number": report.revision_number, "revision_id": str(latest.pk) if latest else None,
            "source_fingerprint": report.source_fingerprint, "current_source_token": report_source_token(report),
            "created_by": str(report.created_by_id) if report.created_by_id else None,
            "created_at": report.created_at.isoformat(),
            "pages": sorted({span.document_page.page_number for span in spans}),
            "spans": [{"id": str(span.pk), "page": span.document_page.page_number,
                       "page_id": str(span.document_page_id), "ocr_block_id": str(span.ocr_block_id) if span.ocr_block_id else None,
                       "start_offset": span.start_offset, "end_offset": span.end_offset, "boundary_basis": span.boundary_basis}
                      for span in spans],
            "reason": "来源不可用" if unavailable or damaged or not spans else "旧解析报告，请重新分段核对" if historical
                      else "整份报告已排除" if status == "EXCLUDED" else ""}


def base_field_source_token(fact, *, report_token=None):
    return digest({"report": report_token or report_source_token(fact.clinical_report), "field_key": fact.field_key,
                   "entity_key": fact.entity_key, "schema_version": fact.schema_version,
                   "automatic_content": fact.automatic_content, "raw_text": fact.raw_text,
                   "fragments": [(fragment.ordinal, str(fragment.document_page_id), str(fragment.ocr_block_id),
                                  fragment.start_offset, fragment.end_offset, fragment.raw_text, fragment.polygon,
                                  fragment.ocr_block.text if fragment.ocr_block_id else None)
                                 for fragment in fact.source_fragments.all()]})


def field_source_token(fact):
    from .clinical_context import ContextResolver, has_context

    if has_context(fact):
        return ContextResolver(fact.clinical_report).evaluate(fact)["token"]
    return base_field_source_token(fact)


def fragment_sources(fact):
    from django.urls import reverse

    values = []
    for fragment in fact.source_fragments.select_related("document_page", "evidence"):
        path = reverse("documents:document_viewer", args=[fact.document_id]) + f"?page={fragment.document_page.page_number}"
        if fact.parsing_version_id and fact.parsing_version.active and fragment.evidence_id:
            path += f"&evidence={fragment.evidence_id}"
        values.append({
            "id": str(fragment.pk), "fact_id": str(fact.pk), "report_id": str(fact.clinical_report_id),
            "document_id": str(fact.document_id), "page": fragment.document_page.page_number,
            "page_id": str(fragment.document_page_id), "parsing_version": str(fact.parsing_version_id) if fact.parsing_version_id else None,
            "evidence_id": str(fragment.evidence_id) if fragment.evidence_id else None,
            "ocr_block_id": str(fragment.ocr_block_id) if fragment.ocr_block_id else None,
            "ordinal": fragment.ordinal, "source_kind": fragment.source_kind,
            "start_offset": fragment.start_offset, "end_offset": fragment.end_offset,
            "raw_text": fragment.raw_text, "polygon": fragment.polygon,
            "location": "REGION" if fragment.polygon else "PAGE", "url": path,
        })
    return values


def effective_field(fact, *, context_resolver=None):
    from .readmodels import source_info

    report = report_state(fact.clinical_report)
    from .clinical_context import ContextResolver, has_context

    context = (context_resolver or ContextResolver(fact.clinical_report)).evaluate(fact) if has_context(fact) else None
    token = context["token"] if context else base_field_source_token(fact)
    latest = fact.revisions.order_by("-sequence").first()
    state = deepcopy(latest.after) if latest else {"content": deepcopy(fact.automatic_content), "status": "PENDING", "source_token": token}
    fragments = list(fact.source_fragments.select_related("ocr_block", "document_page", "evidence"))
    invalid = not report["source_valid"] or not fragments
    for fragment in fragments:
        try:
            fragment.clean()
        except (ValidationError, ValueError, TypeError, AttributeError):
            invalid = True
    if fact.origin == "AUTOMATIC" and (not fact.evidence_id or fact.evidence.source_text != fact.raw_text):
        invalid = True
    changed = state.get("source_token") != token
    excluded = report["status"] == "EXCLUDED"
    if excluded:
        state["status"] = "EXCLUDED"
    elif (invalid or changed) and not (context and state["status"] == "EXCLUDED"):
        state["status"] = "PENDING"
    context_changed = bool(context and changed)
    if context:
        state.update(context_state="STALE" if context_changed else context["state"],
                     context_snapshot=context["snapshot"], current_semantic_qualifiers=context["semantic_qualifiers"])
    result = {**state, "id": str(fact.pk), "origin": fact.origin, "representation": "FIELD",
            "status_label": {"PENDING": "待核对", "CONFIRMED": "已核对", "DEFERRED": "暂缓", "EXCLUDED": "已排除"}[state["status"]],
            "category": fact.category, "category_label": "分子/基因字段" if fact.clinical_report.routing_kind == "MOLECULAR" else "病理/IHC 字段" if context else "影像字段", "field_key": fact.field_key,
            "field_label": FIELDS[fact.field_key].label, "entity_key": fact.entity_key,
            "report_id": str(fact.clinical_report_id), "schema_version": fact.schema_version,
            "source": source_info(fact), "fragments": fragment_sources(fact),
            "revision_number": fact.revision_number, "revision_id": str(latest.pk) if latest else None,
            "inherited_from": [], "historical": report["historical"],
            "source_valid": not invalid and not excluded,
            "usable": state["status"] == "CONFIRMED" and not (invalid or changed or excluded) and (context is None or context["qualified"]),
            "reason": report["reason"] if invalid or excluded else "来源内容已变化，请重新核对" if changed else {
                "PENDING": "尚未核对", "DEFERRED": "暂不处理", "EXCLUDED": "已排除",
            }.get(state["status"], ""), "current_source_token": token}
    if context:
        result["created_by"] = str(fact.created_by_id) if fact.created_by_id else None
        result["revision_author_tuples"] = [(str(r.pk), r.sequence, r.action, str(r.author_id) if r.author_id else None)
                                           for r in fact.revisions.order_by("sequence")]
        if not (invalid or changed or excluded) and context["state"] != "RESOLVED":
            result["reason"] = context["reason"]
            if state["status"] == "CONFIRMED":
                result["status_label"] = "原文已核对，检测/标本未关联" if context["state"] == "UNLINKED" else "原文已核对，关联待核对"
        elif not (invalid or changed or excluded) and not context["qualified"]:
            result["reason"] = context["reason"]
            if state["status"] == "CONFIRMED":
                result["status_label"] = ("原文已核对，身份或范围待核对" if fact.schema_version == "MOLECULAR_REPORT_V1" and state["content"].get("source_role") in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA", "REPORT_DRUG_EVIDENCE"}
                                          else "原文已核对，非本次结果")
    if fact.schema_version == "MOLECULAR_REPORT_V1":
        from .molecular_presentation import details
        result["molecular_details"] = details(result["content"])
    return result


def report_material(patient, *, document_ids=None, report_ids=None, include_history=False):
    """Trusted projection called only inside an authorized document/patient scope."""
    query = report_queryset().filter(document__patient=patient, document__deleted_at__isnull=True)
    if document_ids is not None:
        query = query.filter(document_id__in=document_ids)
    if report_ids is not None:
        query = query.filter(pk__in=report_ids)
    result = []
    for report in query:
        row = report_state(report)
        if row["historical"] and not include_history:
            continue
        from .clinical_context import ContextResolver

        resolver = ContextResolver(report) if report.routing_kind in {"PATHOLOGY", "MOLECULAR"} else None
        fields = [effective_field(fact, context_resolver=resolver) for fact in report.fields.select_related(
            "document__patient__account", "document_page", "parsing_version", "evidence", "clinical_report__document__patient__account", "clinical_report__parsing_version",
        ).prefetch_related("source_fragments__ocr_block").order_by("reading_order", "pk")]
        if report.routing_kind == "MOLECULAR":
            order = {"specimen": 0, "assay": 1, "ihc": 2, "variant": 3, "drug_evidence": 4}
            names = {"specimen": "标本", "assay": "检测 / panel", "ihc": "免疫组化", "variant": "完整变异", "drug_evidence": "报告药物依据"}
            fields.sort(key=lambda f: (order.get(FIELDS[f["field_key"]].entity_kind, 5), f["entity_key"], FIELDS[f["field_key"]].rank, f["id"]))
            groups, counts = {}, {}
            for field in fields:
                kind = FIELDS[field["field_key"]].entity_kind
                if field["entity_key"] not in groups:
                    counts[kind] = counts.get(kind, 0) + 1
                    groups[field["entity_key"]] = f"第 {counts[kind]} 个{names.get(kind, '报告分组')}"
                field["group_title"] = groups[field["entity_key"]]
        for field in fields:
            from .molecular_schema import COMPONENT_KEYS
            repeat_component = field["field_key"] in COMPONENT_KEYS - {"variant.tier"}
            field["conflict"] = any(other["usable"] and field["usable"] and other["id"] != field["id"]
                                    and other["entity_key"] == field["entity_key"] and other["field_key"] == field["field_key"]
                                    and other["content"]["value"].get("score_kind") == field["content"]["value"].get("score_kind")
                                    and other["content"]["value"].get("measurement_role") == field["content"]["value"].get("measurement_role")
                                    and not repeat_component
                                    and (other["content"]["value"] != field["content"]["value"]
                                         or other["content"].get("reported_assertion", {}).get("code") != field["content"].get("reported_assertion", {}).get("code")) for other in fields)
        row["fields"] = fields
        row["date_values"] = [f["content"]["value"] for f in fields if f["field_key"] == "report.exam_date"]
        row["date_conflict"] = any(f["conflict"] for f in fields if f["field_key"] == "report.exam_date")
        result.append(row)
    return result


def review_reports(patient, *, actor, document_id=None, report_ids=None, include_history=False):
    access = authorize_patient(patient, actor)
    return report_material(access.patient, document_ids=[document_id] if document_id else None,
                           report_ids=report_ids, include_history=include_history)
