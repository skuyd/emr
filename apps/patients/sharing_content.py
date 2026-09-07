"""Explicit public projection of a private snapshot; omitted content stays private."""

from copy import deepcopy

from apps.exports.content import SECTIONS
from apps.exports.clinical import FIELD_CONTENT
from apps.exports.errors import ExportInputError
from apps.exports.selection import identifiers


PARTIAL_KEYS = ("fact_ids", "lab_ids", "report_ids", "clinical_field_ids")


def normalize_scope(selection):
    if not isinstance(selection, dict):
        raise ExportInputError("请选择要分享的资料和内容。")
    scope = {"mode": "documents", "document_ids": identifiers(selection.get("document_ids", []))}
    records = identifiers(selection.get('self_record_ids', []))
    if records:
        scope['self_record_ids'] = records
    if not scope["document_ids"] and not records:
        raise ExportInputError("请至少选择一份资料或一条日常记录。")
    sections = selection.get("sections")
    if not isinstance(sections, list) or not sections or set(sections) - {key for key, _ in SECTIONS}:
        raise ExportInputError("请明确选择分享的展示范围。")
    scope["sections"] = list(dict.fromkeys(sections))
    if records and 'self_records' not in sections:
        raise ExportInputError('请选择日常记录展示范围。')
    if not scope['document_ids'] and 'sources' in sections:
        raise ExportInputError('本次没有上传原件，请取消原件来源范围。')
    for key in PARTIAL_KEYS:
        if key in selection:
            scope[key] = identifiers(selection[key])
            if not scope[key]:
                raise ExportInputError("精细内容选择不能为空。")
    if any(key in scope for key in PARTIAL_KEYS) and "sources" in sections:
        raise ExportInputError("精细内容分享不能同时开放整份原件；请另建资料分享。")
    return scope


def project_snapshot(snapshot, scope):
    sections = set(scope["sections"])
    categories = set()
    if "diagnosis" in sections:
        categories.update(("DIAGNOSIS", "STAGE"))
    if "treatment" in sections:
        categories.add("TREATMENT")
    if "imaging" in sections:
        categories.update(("IMAGING", "PATHOLOGY"))
    facts = [deepcopy(row) for row in snapshot["facts"] if row["category"] in categories]
    partial = any(key in scope for key in PARTIAL_KEYS)
    if partial:
        chosen = set(scope.get("fact_ids", []))
        facts = [row for row in facts if row["id"] in chosen]
    for row in facts:
        row["source"] = {key: deepcopy(value) for key, value in row["source"].items() if key in {"document_id", "page", "polygon", "location"}}
    lab_ids = set(snapshot["card"]["lab_ids"]) if "labs" in sections else set()
    labs = [deepcopy(row) for row in snapshot["labs"] if row["id"] in lab_ids]
    if partial:
        chosen = set(scope.get("lab_ids", []))
        labs = [row for row in labs if row["id"] in chosen]
    projected = {
        "schema_version": snapshot["schema_version"], "patient_id": snapshot["patient_id"],
        "generated_at": snapshot["generated_at"], "dependency_fingerprint": snapshot["dependency_fingerprint"],
        "selection": deepcopy(scope), "documents": deepcopy(snapshot["documents"]),
        "patient": deepcopy(snapshot["patient"]) if "patient" in sections else {},
        "facts": facts, "labs": labs,
        'self_record_fingerprint': snapshot.get('self_record_fingerprint'),
    }
    record_ids = set(scope.get('self_record_ids', [])) if 'self_records' in sections else set()
    selected_records = [row for row in snapshot.get('self_records', []) if row['id'] in record_ids]
    if record_ids != {row['id'] for row in selected_records}:
        raise ExportInputError('部分选定日常记录已变化。')
    # Sharing a daily record exposes only its current entry, never its author's
    # account identity, old revisions or ordinary authenticated history URL.
    data_keys = {'schema_version', 'kind', 'measured_at', 'local_time', 'measured_local_raw', 'timezone', 'utc_offset',
                 'time_precision', 'raw_value', 'raw_unit', 'normalized_value', 'normalized_unit', 'conversion',
                 'symptom_name', 'severity', 'notes', 'source_label', 'source_kind'}
    projected['self_records'] = [{
        **{key: deepcopy(row[key]) for key in ('id', 'kind', 'kind_label', 'origin', 'revision_number')},
        'data': {key: deepcopy(value) for key, value in row['data'].items() if key in data_keys},
    } for row in selected_records]
    # Typed clinical projection shares the export contract. Filter fields by
    # their declared display category first, then derive reports and sources;
    # merely selecting a report never releases its unselected body.
    documents = set(scope["document_ids"])
    reports = [deepcopy(row) for row in snapshot.get("clinical_reports", []) if row.get("document_id") in documents]
    if "report_ids" in scope:
        wanted = set(scope["report_ids"])
        if wanted - {row["id"] for row in reports}:
            raise ExportInputError("部分选定报告已变化。")
        reports = [row for row in reports if row["id"] in wanted]
    report_ids = {row["id"] for row in reports}
    fields = [deepcopy(row) for row in snapshot.get("clinical_fields", []) if row.get("report_id") in report_ids]
    if "clinical_field_ids" in scope:
        wanted = set(scope["clinical_field_ids"])
        if wanted - {row["id"] for row in fields}:
            raise ExportInputError("部分选定字段已变化。")
        fields = [row for row in fields if row["id"] in wanted]
    elif partial and "report_ids" not in scope:
        fields = []
    fields = [row for row in fields if row.get("category") in categories]
    for row in fields:
        row["content"] = {key: deepcopy(value) for key, value in row.get("content", {}).items() if key in FIELD_CONTENT}
        row["content"]["source_context_omitted"] = True
        row["source"] = {key: deepcopy(value) for key, value in row.get("source", {}).items()
                         if key in {"document_id", "page", "polygon", "location", "evidence_id"}}
    used_reports = {row["report_id"] for row in fields}
    field_ids = {row["id"] for row in fields}
    reports = [{key: deepcopy(value) for key, value in row.items()
                if key in {"id", "document_id", "parsing_version", "routing_kind", "title", "pages"}}
               for row in reports if row["id"] in used_reports]
    sources = [{key: deepcopy(value) for key, value in row.items() if key not in {"raw_text", "url", "text", "source_text"}}
               for row in snapshot.get("clinical_field_sources", [])
               if row.get("fact_id") in field_ids and row.get("report_id") in used_reports and row.get("document_id") in documents]
    projected.update(clinical_reports=reports, clinical_fields=fields, clinical_field_sources=sources)
    return projected
