"""Explicit public projection of a private snapshot; omitted content stays private."""

from copy import deepcopy

from apps.exports.content import SECTIONS
from apps.exports.clinical import FIELD_CONTENT
from apps.exports.errors import ExportInputError
from apps.exports.selection import identifiers
from apps.exports.treatment import ARRAYS as DERIVED_ARRAYS, SELECTION_KEYS as DERIVED_KEYS, normalized_selection


PARTIAL_KEYS = ("fact_ids", "lab_ids", "observation_ids", "report_ids", "clinical_field_ids", 'lesion_ids', *DERIVED_KEYS)


def normalize_scope(selection):
    if not isinstance(selection, dict):
        raise ExportInputError("请选择要分享的资料和内容。")
    scope = {"mode": "documents", "document_ids": identifiers(selection.get("document_ids", []))}
    records = identifiers(selection.get('self_record_ids', []))
    if records:
        scope['self_record_ids'] = records
    glucose = identifiers(selection.get('glucose_record_ids', []))
    if glucose:
        scope['glucose_record_ids'] = glucose
    derived = normalized_selection(selection)
    has_derived = any(derived[key] for key in DERIVED_KEYS)
    if not scope["document_ids"] and not records and not glucose and not has_derived:
        raise ExportInputError("请至少选择一份资料、一条日常或血糖记录、或有效治疗补记。")
    sections = selection.get("sections")
    if not isinstance(sections, list) or not sections or set(sections) - {key for key, _ in SECTIONS}:
        raise ExportInputError("请明确选择分享的展示范围。")
    scope["sections"] = list(dict.fromkeys(sections))
    if records and 'self_records' not in sections:
        raise ExportInputError('请选择日常记录展示范围。')
    if glucose and 'glucose' not in sections:
        raise ExportInputError('请选择血糖记录展示范围。')
    if selection.get('lesion_ids') and 'imaging' not in sections:
        raise ExportInputError('请选择病灶观察所属的影像展示范围。')
    if any(derived[key] for key in ("treatment_event_ids", "regimen_ids", "cycle_ids")) and "treatment" not in sections:
        raise ExportInputError("请选择治疗展示范围。")
    if derived["personal_change_ids"] and "labs" not in sections:
        raise ExportInputError("请选择个人变化所属的检验展示范围。")
    if not scope['document_ids'] and 'sources' in sections:
        raise ExportInputError('本次没有上传原件，请取消原件来源范围。')
    for key in PARTIAL_KEYS:
        if key in selection:
            chosen = identifiers(selection[key])
            if not chosen and key in DERIVED_KEYS:
                continue
            scope[key] = chosen
            if not chosen:
                raise ExportInputError("精细内容选择不能为空。")
    if has_derived:
        scope.update({key: derived[key] for key in ("cycle_mode", "cycle_metric_codes", "include_pending_cycles")})
    if any(key in scope for key in PARTIAL_KEYS) and "sources" in sections:
        raise ExportInputError("精细内容分享不能同时开放整份原件；请另建资料分享。")
    return scope


def project_snapshot(snapshot, scope):
    from apps.cloud_imaging.projection import assert_safe_snapshot

    assert_safe_snapshot(snapshot)
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
        chosen = set(scope.get("lab_ids", scope.get("observation_ids", [])))
        if "observation_ids" in scope:
            chosen &= set(scope["observation_ids"])
        labs = [row for row in labs if row["id"] in chosen]
    projected = {
        "schema_version": snapshot["schema_version"], "patient_id": snapshot["patient_id"],
        "generated_at": snapshot["generated_at"], "dependency_fingerprint": snapshot["dependency_fingerprint"],
        "selection": deepcopy(scope), "documents": deepcopy(snapshot["documents"]),
        "patient": deepcopy(snapshot["patient"]) if "patient" in sections else {},
        "facts": facts, "labs": labs,
        'self_record_fingerprint': snapshot.get('self_record_fingerprint'),
        "treatment_fingerprint": snapshot.get("treatment_fingerprint"),
        "treatment_binding_ids": deepcopy(snapshot.get("treatment_binding_ids", {})),
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
    from apps.glucose.output import share_material as glucose_share_material
    projected.update(glucose_share_material(snapshot, scope))
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
        if row.get('laterality_scope'):
            side = row['laterality_scope']
            if side.get('parent_field_id') not in {field['id'] for field in fields}:
                side.update(parent_selected=False, parent_field_id=None)
    used_reports = {row["report_id"] for row in fields}
    field_ids = {row["id"] for row in fields}
    reports = [{key: deepcopy(value) for key, value in row.items()
                if key in {"id", "document_id", "parsing_version", "routing_kind", "title", "pages"}}
               for row in reports if row["id"] in used_reports]
    sources = [{key: deepcopy(value) for key, value in row.items() if key not in {"raw_text", "url", "text", "source_text"}}
               for row in snapshot.get("clinical_field_sources", [])
               if row.get("fact_id") in field_ids and row.get("report_id") in used_reports and row.get("document_id") in documents]
    projected.update(clinical_reports=reports, clinical_fields=fields, clinical_field_sources=sources)
    from apps.lesions.portable import shared_material as lesion_shared_material
    projected.update(lesion_shared_material(snapshot, fields, scope))
    projected.update({key: deepcopy(snapshot.get(key, [])) for key in DERIVED_ARRAYS})
    if "treatment" not in sections:
        for key in ("treatment_events", "treatment_regimens", "treatment_cycles", "cycle_links", "cycle_points", "cycle_key_nodes"):
            projected[key] = []
    if "labs" not in sections:
        for key in ("cycle_points", "cycle_key_nodes", "personal_changes"):
            projected[key] = []
        for row in projected["treatment_regimens"]:
            row["content"]["lab_periodicity"] = []
        projected["cycle_links"] = [row for row in projected["cycle_links"] if row["kind"] == "event"]
    source_ids = {identity for key in ("treatment_events", "cycle_points", "personal_changes")
                  for row in projected[key] for identity in row.get("source_ids", [])}
    source_ids.update(identity for regimen in projected["treatment_regimens"]
                      for auxiliary in regimen["content"].get("lab_periodicity", []) for identity in auxiliary.get("source_ids", []))
    projected["derived_sources"] = [row for row in projected["derived_sources"] if row["id"] in source_ids]
    return projected
