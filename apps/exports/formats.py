"""Portable, linked exports. No live queries are used for structured content."""

from copy import deepcopy
import csv
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import zipfile

from apps.documents.errors import IntegrityMismatch
from apps.documents.models import Document

from .errors import ExportInputError, SnapshotChanged


FORMAT_CHOICES = (("pdf", "速查卡 PDF"), ("original", "单份原件"), ("json", "结构化 JSON"),
                  ("csv", "关联 CSV 表格（ZIP）"), ("zip", "资料包 ZIP"))
PART_CHOICES = (("pdf", "速查卡 PDF"), ("originals", "原始文件"), ("csv", "结构化 CSV"), ("json", "结构化 JSON"))


@dataclass(frozen=True)
class Artifact:
    payload: bytes
    filename: str
    content_type: str


def validate_options(options, snapshot):
    if not isinstance(options, dict) or options.get("format") not in dict(FORMAT_CHOICES):
        raise ExportInputError("请选择有效的导出格式。")
    kind = options["format"]
    if kind == "original" and len(snapshot["documents"]) != 1:
        raise ExportInputError("单份原件下载需选择一份资料；多份请选择资料包和原始文件。")
    parts = options.get("parts", [])
    if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
        raise ExportInputError("资料包内容选择无效。")
    if kind == "zip" and (not parts or set(parts) - dict(PART_CHOICES).keys()):
        raise ExportInputError("请至少选择一种资料包内容。")
    return {"format": kind, "parts": sorted(set(parts)) if kind == "zip" else []}


def structured_data(snapshot):
    result = deepcopy({key: snapshot[key] for key in (
        "schema_version", "generated_at", "patient", "documents", "facts", "labs", "sources",
    )})
    for fact in result["facts"]:
        fact["source"].pop("url", None)
    result["scope"] = {key: deepcopy(snapshot["selection"].get(key)) for key in (
        "mode", "document_ids", "start", "end", "unknown_ids",
    )}
    result["exclusions"] = {
        "documents": [{"id": item["id"], "reason": item["reason"]} for item in snapshot["excluded_documents"]],
        "facts": deepcopy(snapshot["excluded_facts"]),
        "card_labs": deepcopy(snapshot["excluded_card_labs"]),
    }
    result["semantics"] = {
        "facts": "Only confirmed facts with currently valid sources.",
        "labs": "Current effective results, including limited or suspect values with their quality flags.",
        "missing": "null is missing; it is never zero. Original strings are preserved.",
        "dates": "DAY, MONTH, YEAR or UNKNOWN; incomplete dates must not be treated as exact days.",
    }
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def json_bytes(snapshot):
    return (_json(structured_data(snapshot)) + "\n").encode("utf-8")


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = _json(value)
    elif isinstance(value, bool):
        value = "true" if value else "false"
    else:
        value = str(value)
    # Defend all text columns, including leading whitespace/control characters.
    candidate = value
    while candidate and (candidate[0].isspace() or ord(candidate[0]) < 32 or candidate[0] == "\ufeff"):
        candidate = candidate[1:]
    if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _csv(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_cell(row.get(field)) for field in fields})
    return output.getvalue().encode("utf-8-sig")


def csv_tables(snapshot):
    data = structured_data(snapshot)
    facts = [{
        "id": row["id"], "document_id": row["source"]["document_id"], "page": row["source"]["page"],
        "source_id": "fact:" + row["id"], "parsing_version": row["source"]["parsing_version"],
        "category": row["category"], "text": row["content"]["text"], "content": row["content"],
        "origin": row["origin"], "status": row["status"], "revision_number": row["revision_number"], "revision_id": row["revision_id"],
    } for row in data["facts"]]
    labs = [{**row, "date_value": row["date"]["value"], "date_precision": row["date"]["precision"],
             "date_raw": row["date"]["raw"]} for row in data["labs"]]
    # Even empty entity tables have a documented stable header.
    fields = {
        "documents": ["id", "filename", "content_type", "byte_size", "page_count", "sha256", "status",
                      "lifecycle_revision", "parsing_version", "document_type", "date", "date_precision", "date_raw", "institution"],
        "facts": ["id", "document_id", "page", "source_id", "parsing_version", "category", "text", "content",
                  "origin", "status", "revision_number", "revision_id"],
        "labs": ["id", "document_id", "parsing_version", "page", "evidence_id", "raw_name", "name", "standard_code",
                 "standard_name", "raw_value", "value", "raw_result_type", "result_type", "comparator", "numeric_value",
                 "raw_unit", "unit", "date", "date_value", "date_precision", "date_raw", "institution", "specimen", "method",
                 "reference_range_raw", "reference_range", "reference_definition", "reference_label", "raw_report_flag",
                 "quality_issues", "review_state", "value_origin", "capability_level", "revision_number", "revision_id",
                 "dictionary_version", "mapping_dictionary_version", "quality_rule_version", "normalization_candidates",
                 "comparison", "card_eligible", "field_sources", "field_evidence"],
        "sources": ["id", "document_id", "page", "parsing_version", "raw_text", "polygon", "location",
                    "confidence", "filename", "page_id", "evidence_id", "sha256"],
    }
    entities = {"documents": data["documents"], "facts": facts, "labs": labs, "sources": data["sources"]}
    output = {key + ".csv": _csv(rows, fields[key]) for key, rows in entities.items()}
    output["schema.json"] = (_json({
        "schema_version": data["schema_version"], "generated_at": data["generated_at"], "fields": fields,
        "scope": data["scope"], "exclusions": data["exclusions"], "semantics": data["semantics"],
        "csv": {"encoding": "UTF-8 with BOM", "null": "empty cell",
                "nested_values": "JSON", "formula_defense": "A leading apostrophe is added to risky text; JSON preserves original strings."},
        "relations": ["facts.document_id -> documents.id", "facts.source_id -> sources.id",
                      "labs.document_id -> documents.id", "labs.evidence_id -> sources.id",
                      "sources.document_id -> documents.id"],
    }) + "\n").encode("utf-8")
    return output


def _original(document):
    current = Document.objects.filter(pk=document["id"], deleted_at__isnull=True).first()
    if current is None or current.sha256 != document["sha256"] or current.byte_size != document["byte_size"]:
        raise SnapshotChanged("原件已变化，请重新选择。")
    return current


def _original_bytes(document, store):
    current = _original(document)
    from apps.documents.errors import UploadDomainError

    try:
        with store.open_private(current.original_object_key) as stream:
            # Bound reads by the recorded size. Oversized and truncated objects both fail.
            payload = stream.read(current.byte_size + 1)
        if len(payload) != current.byte_size or hashlib.sha256(payload).hexdigest() != current.sha256:
            raise IntegrityMismatch()
    except UploadDomainError as error:
        error.document_id = current.pk
        raise
    return payload


def _archive(entries, snapshot):
    manifest = {
        "schema_version": snapshot["schema_version"], "generated_at": snapshot["generated_at"],
        "document_ids": [item["id"] for item in snapshot["documents"]], "files": [],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
        for path, payload, metadata in entries:
            bundle.writestr(path, payload)
            manifest["files"].append({
                "path": path, "byte_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), **metadata,
            })
        bundle.writestr("manifest.json", (_json(manifest) + "\n").encode("utf-8"))
    return output.getvalue()


def _filename(document):
    # Stable UUID disambiguation also handles duplicate, path-like and reserved names.
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", document["filename"]).strip(" .") or "original"
    if len(name) > 140:
        suffix = PurePosixPath(name).suffix[:12]
        name = name[:120] + suffix
    return "originals/" + document["id"] + "-" + name


def build_artifact(snapshot, options, store):
    from .pdf import render_pdf

    options = validate_options(options, snapshot)
    kind, parts = options["format"], options["parts"]
    if kind == "original":
        document = snapshot["documents"][0]
        return Artifact(_original_bytes(document, store), document["filename"], document["content_type"])
    if kind == "json":
        return Artifact(json_bytes(snapshot), "records.json", "application/json")
    if kind == "pdf":
        return Artifact(render_pdf(snapshot), "visit-card.pdf", "application/pdf")
    entries = []
    metadata = {"document_ids": [item["id"] for item in snapshot["documents"]]}
    if kind == "csv" or "csv" in parts:
        entries.extend(("csv/" + name, payload, {"kind": "csv", **metadata})
                       for name, payload in csv_tables(snapshot).items())
    if "json" in parts:
        entries.append(("records.json", json_bytes(snapshot), {"kind": "json", **metadata}))
    if "pdf" in parts:
        entries.append(("visit-card.pdf", render_pdf(snapshot), {"kind": "pdf", **metadata}))
    if "originals" in parts:
        for document in snapshot["documents"]:
            entries.append((_filename(document), _original_bytes(document, store), {
                "kind": "original", "document_id": document["id"], "original_filename": document["filename"],
            }))
    if not entries:
        raise ExportInputError("没有可生成的内容。")
    return Artifact(_archive(entries, snapshot), "records-csv.zip" if kind == "csv" else "records.zip", "application/zip")
