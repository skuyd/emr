"""Selected clinical material shared by portable exports and scoped viewers."""

from copy import deepcopy

from .errors import ExportInputError
from .selection import identifiers
from . import pathology, molecular


REPORT_FIELDS = ("id", "document_id", "parsing_version", "origin", "title", "routing_kind", "ordinal",
                 "schema_version", "segmenter_version", "boundary_state", "limitations", "status",
                 "revision_number", "revision_id", "source_fingerprint", "created_by", "created_at", "pages", "spans")
FIELD_FIELDS = ("id", "report_id", "entity_key", "field_key", "field_label", "schema_version", "origin",
                "category", "category_label", "content", "status", "revision_number", "revision_id", "conflict", "source")
SOURCE_FIELDS = ("id", "fact_id", "report_id", "document_id", "page", "page_id", "parsing_version", "evidence_id",
                 "ocr_block_id", "ordinal", "source_kind", "start_offset", "end_offset", "raw_text", "polygon", "location")
FIELD_CONTENT = ("category", "text", "date", "date_raw", "date_precision", "institution", "record_date", "dates",
                 "limitations", "schema_version", "field_key", "value_type", "result_type", "value")


def clinical_projection(material, selection):
    reports = [row for row in material if row["source_valid"] and row["status"] == "ACTIVE"]
    if selection.get("report_ids") is not None:
        chosen = identifiers(selection["report_ids"])
        if set(chosen) - {row["id"] for row in reports}:
            raise ExportInputError("部分报告范围已失效，请重新选择。")
        reports = [row for row in reports if row["id"] in chosen]
    fields = [field for report in reports for field in report["fields"] if field["usable"]]
    if selection.get("clinical_field_ids") is not None:
        chosen = identifiers(selection["clinical_field_ids"])
        if set(chosen) - {row["id"] for row in fields}:
            raise ExportInputError("部分字段尚未核对、不属于所选报告或已失效。")
        fields = [row for row in fields if row["id"] in chosen]
        reports = [row for row in reports if row["id"] in {field["report_id"] for field in fields}]
    projected_fields = [{key: deepcopy(field[key]) for key in FIELD_FIELDS} for field in fields]
    contexts = pathology.capture_context(fields)
    molecular_contexts = molecular.capture_context(fields, material)
    for field in projected_fields:
        field["source"].pop("url", None)
    result = {
        "clinical_reports": [{key: deepcopy(report[key]) for key in REPORT_FIELDS} for report in reports],
        "clinical_fields": projected_fields,
        "clinical_field_sources": [{key: deepcopy(source[key]) for key in SOURCE_FIELDS}
                                    for field in fields for source in field["fragments"]],
    }
    if selection.get("clinical_field_ids") is not None:
        # The selected field value is portable; its original whole clause may
        # contain other fields. Detailed transcription audit remains in the
        # authorized fact page or an explicitly selected whole report.
        for field in result["clinical_fields"]:
            field["content"] = {key: field["content"][key] for key in FIELD_CONTENT}
            field["content"]["source_context_omitted"] = True
            field["source"]["raw_text"] = ""
        for source in result["clinical_field_sources"]:
            source.update(raw_text="", start_offset=None, end_offset=None)
        for report in result["clinical_reports"]:
            report["spans"] = []
            report["pages"] = sorted({s["page"] for s in result["clinical_field_sources"] if s["report_id"] == report["id"]})
    aliases = molecular.selection_aliases(result["clinical_fields"], contexts, molecular_contexts) if molecular_contexts else None
    result["clinical_fields"] = pathology.project_fields(result["clinical_fields"], contexts, selection, scope_aliases=aliases)
    result["clinical_fields"] = molecular.project_fields(result["clinical_fields"], molecular_contexts, selection, scope_aliases=aliases)
    pathology.redact_sources(result["clinical_field_sources"], result["clinical_fields"])
    result["clinical_reports"] = pathology.project_reports(result["clinical_reports"], result["clinical_fields"], result["clinical_field_sources"])
    molecular.redact_sources(result["clinical_field_sources"], result["clinical_fields"])
    result["clinical_reports"] = molecular.project_reports(result["clinical_reports"], result["clinical_fields"], result["clinical_field_sources"])
    result[pathology.PRIVATE_CONTEXT] = contexts
    result[molecular.PRIVATE_CONTEXT] = molecular_contexts
    return result
