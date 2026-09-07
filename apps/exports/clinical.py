"""Selected clinical material shared by portable exports and scoped viewers."""

from copy import deepcopy

from .errors import ExportInputError
from .selection import identifiers


REPORT_FIELDS = ("id", "document_id", "parsing_version", "origin", "title", "routing_kind", "ordinal",
                 "schema_version", "segmenter_version", "boundary_state", "limitations", "status",
                 "revision_number", "revision_id", "source_fingerprint", "created_by", "created_at", "pages", "spans")
FIELD_FIELDS = ("id", "report_id", "entity_key", "field_key", "field_label", "schema_version", "origin",
                "category", "category_label", "content", "status", "revision_number", "revision_id", "conflict", "source")
SOURCE_FIELDS = ("id", "fact_id", "report_id", "document_id", "page", "page_id", "parsing_version", "evidence_id",
                 "ocr_block_id", "ordinal", "source_kind", "start_offset", "end_offset", "raw_text", "polygon", "location")


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
    for field in projected_fields:
        field["source"].pop("url", None)
    return {
        "clinical_reports": [{key: deepcopy(report[key]) for key in REPORT_FIELDS} for report in reports],
        "clinical_fields": projected_fields,
        "clinical_field_sources": [{key: deepcopy(source[key]) for key in SOURCE_FIELDS}
                                    for field in fields for source in field["fragments"]],
    }
