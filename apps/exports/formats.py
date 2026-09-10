"""Portable, linked exports. No live queries are used for structured content."""

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import zipfile

from apps.documents.models import Document
from apps.documents.storage import _copy_verified

from .errors import ExportInputError, SnapshotChanged
from .files import Artifact, private_temporary_file


FORMAT_CHOICES = (("pdf", "速查卡 PDF"), ("original", "单份原件"), ("json", "结构化 JSON"),
                  ("csv", "关联 CSV 表格（ZIP）"), ("zip", "资料包 ZIP"))
PART_CHOICES = (("pdf", "速查卡 PDF"), ("originals", "原始文件"), ("csv", "结构化 CSV"), ("json", "结构化 JSON"))


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
    if kind == 'zip' and 'originals' in parts and not snapshot['documents']:
        raise ExportInputError('本次没有上传原件，请选择速查卡或结构化内容。')
    return {"format": kind, "parts": sorted(set(parts)) if kind == "zip" else []}


def structured_data(snapshot):
    from apps.cloud_imaging.projection import assert_safe_snapshot

    assert_safe_snapshot(snapshot)
    result = deepcopy({key: snapshot[key] for key in (
        "schema_version", "generated_at", "patient", "documents", "facts", "labs", "sources",
    )})
    for fact in result["facts"]:
        fact["source"].pop("url", None)
    result['self_records'] = deepcopy(snapshot.get('self_records', []))
    for row in result['self_records']:
        row['source'].pop('url', None)
    for key in ("clinical_reports", "clinical_fields", "clinical_field_sources"):
        result[key] = deepcopy(snapshot.get(key, []))
    from apps.glucose.output import ARRAYS as GLUCOSE_ARRAYS
    for key in GLUCOSE_ARRAYS:
        result[key] = deepcopy(snapshot.get(key, []))
    from apps.cloud_imaging.output import ARRAYS as CLOUD_ARRAYS
    for key in CLOUD_ARRAYS:
        result[key] = deepcopy(snapshot.get(key, []))
    from .treatment import ARRAYS, SELECTION_KEYS
    for key in ARRAYS:
        result[key] = deepcopy(snapshot.get(key, []))
    result["scope"] = {key: deepcopy(snapshot["selection"].get(key)) for key in (
        "mode", "document_ids", "start", "end", "unknown_ids", "report_ids", "clinical_field_ids", "fact_ids", "observation_ids",
        "self_record_ids", "glucose_record_ids", "cloud_source_ids",
        "semantic_unit_policy",
    )}
    result["exclusions"] = {
        "documents": [{"id": item["id"], "reason": item["reason"]} for item in snapshot["excluded_documents"]],
        "facts": deepcopy(snapshot["excluded_facts"]),
        "card_labs": deepcopy(snapshot["excluded_card_labs"]),
    }
    result["semantics"] = {
        "facts": "Only confirmed facts with currently valid sources.",
        "labs": "Current effective results, including limited or suspect values with their quality flags.",
        "missing": "null is missing; it is never zero. External access strings are explicitly omitted; an omission is not original source text.",
        "dates": "DAY, MONTH, YEAR or UNKNOWN; incomplete dates must not be treated as exact days.",
        "self_records": "Explicitly selected user entries at the effective revision. Raw value/unit, conversion and minute/time zone remain separate. Source IDs refer to daily records, never documents.",
        "glucose_records": "Explicitly selected current measurements and their immutable initial values, actual authors and revisions. Original quantity, exact conversion, sampling/reporting times, precision and unconfirmed time zone remain separate. Unknown time zones never create a UTC instant.",
        "glucose_sources": "Each source row belongs to one selected measurement. Referenced source document/page IDs describe provenance and do not include unselected report content or grant whole-document access.",
        "clinical_fields": "Confirmed fields only. Conflicting values remain separate rows, linked to version-local reports and original source fragments.",
        "clinical_field_scope": "Fine field selection omits whole-clause text and report spans; source identity, page and original geometry remain. Whole report audit requires explicitly selecting the report.",
        "pathology_fields": "PATHOLOGY_IHC_V1 requires its selected semantic unit: reported marker, score kind, original quantity/unit/assertion and selection-local specimen/assay scopes. Aliases grant no lookup access; omitted assay conditions do not establish comparability. Source context and validation closure remain private.",
    }
    result["scope"].update({key: deepcopy(snapshot["selection"].get(key)) for key in
                            (*SELECTION_KEYS, "cycle_mode", "cycle_metric_codes", "include_pending_cycles")})
    result["semantics"]["treatments"] = "Explicit current derived selection, with source and revision identities. Candidate cycles remain PENDING; reported event days are not confirmed medical cycle boundaries."
    result["semantics"]["personal_changes"] = "Calculated from the original full comparable context. Unselected required sources redact the affected values and identities; the baseline is never recomputed on a filtered subset."
    return result


def read_structured_data(payload):
    """Read supported portable versions without modifying a database or old file."""
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        raise ExportInputError("资料JSON格式无效。") from None
    if not isinstance(value, dict) or value.get("schema_version") not in {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5"}:
        raise ExportInputError("不支持该资料格式版本。")
    for key in ("documents", "facts", "labs", "sources"):
        if not isinstance(value.get(key), list):
            raise ExportInputError("资料JSON缺少关联数据表。")
    for key in ("clinical_reports", "clinical_fields", "clinical_field_sources"):
        if key not in value and value["schema_version"] == "1.0":
            value[key] = []
        if not isinstance(value.get(key), list):
            raise ExportInputError("结构化报告关联表无效。")
    if 'self_records' not in value and value['schema_version'] in {'1.0', '1.1'}:
        value['self_records'] = []
    if not isinstance(value.get('self_records'), list):
        raise ExportInputError('日常记录关联表无效。')
    from .treatment import ARRAYS
    for key in ARRAYS:
        if key not in value and value["schema_version"] in {"1.0", "1.1", "1.2"}:
            value[key] = []
        if not isinstance(value.get(key), list):
            raise ExportInputError("治疗与个人变化关联表无效。")
    from apps.glucose.output import ARRAYS as GLUCOSE_ARRAYS
    for key in GLUCOSE_ARRAYS:
        if key not in value and value['schema_version'] in {'1.0', '1.1', '1.2', '1.3'}:
            value[key] = []
        if not isinstance(value.get(key), list):
            raise ExportInputError('血糖记录关联表无效。')
    from apps.cloud_imaging.output import ARRAYS as CLOUD_ARRAYS, validate_portable
    for key in CLOUD_ARRAYS:
        if key not in value and value['schema_version'] in {'1.0','1.1','1.2','1.3','1.4'}:
            value[key] = []
    validate_portable(value)
    from .pathology import validate_portable_fields
    validate_portable_fields(value["clinical_fields"])
    return value


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
    record_meta = ['id', 'kind', 'kind_label', 'origin', 'created_by', 'updated_by', 'created_at', 'updated_at', 'revision_number', 'revision_id', 'source']
    record_values = ['raw_value', 'raw_unit', 'normalized_value', 'normalized_unit', 'conversion', 'measured_at', 'measured_local_raw',
                     'local_time', 'timezone', 'utc_offset', 'time_precision', 'symptom_name', 'severity', 'source_label', 'notes']
    fields['self_records'] = record_meta + record_values
    entities['self_records'] = [{**{key: row.get(key) for key in record_meta}, **{key: row['data'].get(key) for key in record_values}}
                               for row in data['self_records']]
    from .clinical import REPORT_FIELDS, FIELD_FIELDS, SOURCE_FIELDS
    fields.update(clinical_reports=list(REPORT_FIELDS), clinical_fields=list(FIELD_FIELDS), clinical_field_sources=list(SOURCE_FIELDS))
    entities.update({key: data[key] for key in ("clinical_reports", "clinical_fields", "clinical_field_sources")})
    from .treatment import CSV_FIELDS
    fields.update(CSV_FIELDS)
    entities.update({key: data[key] for key in CSV_FIELDS})
    from apps.glucose.output import CSV_FIELDS as GLUCOSE_FIELDS, csv_content as glucose_csv
    fields.update(GLUCOSE_FIELDS)
    entities.update(glucose_csv(data))
    from apps.cloud_imaging.output import CSV_FIELDS as CLOUD_FIELDS
    fields.update(CLOUD_FIELDS)
    entities.update({key: data[key] for key in CLOUD_FIELDS})
    output = {key + ".csv": _csv(rows, fields[key]) for key, rows in entities.items()}
    output["schema.json"] = (_json({
        "schema_version": data["schema_version"], "generated_at": data["generated_at"], "fields": fields,
        "scope": data["scope"], "exclusions": data["exclusions"], "semantics": data["semantics"],
        "csv": {"encoding": "UTF-8 with BOM", "null": "empty cell",
                "nested_values": "JSON", "formula_defense": "A leading apostrophe is added to risky text; JSON preserves original strings."},
        "relations": ["facts.document_id -> documents.id", "facts.source_id -> sources.id",
                      "labs.document_id -> documents.id", "labs.evidence_id -> sources.id",
                      "sources.document_id -> documents.id", "clinical_reports.document_id -> documents.id",
                      "clinical_fields.report_id -> clinical_reports.id", "clinical_field_sources.fact_id -> clinical_fields.id",
                      "clinical_field_sources.report_id -> clinical_reports.id", "clinical_field_sources.document_id -> documents.id",
                      "self_records.source.record_id -> self_records.id",
                      "glucose_record_sources.record_id -> glucose_records.id",
                      "cloud_imaging_evidence.source_id -> cloud_imaging_sources.id",
                      "cloud_imaging_sources.evidence_id -> cloud_imaging_evidence.id; document/page/report identifiers are provenance only",
                      "treatment_cycles.event_ids -> treatment_events.id", "treatment_cycles.regimen_id -> treatment_regimens.id",
                      "cycle_links.cycle_id -> treatment_cycles.id", "cycle_points.cycle_id -> treatment_cycles.id",
                      "cycle_key_nodes.point_id -> cycle_points.id", "cycle_points.source_ids -> derived_sources.id",
                      "personal_changes.source_ids -> derived_sources.id", "treatment_events.source_ids -> derived_sources.id"],
    }) + "\n").encode("utf-8")
    return output


def _original(document):
    current = Document.objects.filter(pk=document["id"], deleted_at__isnull=True).first()
    if current is None or current.sha256 != document["sha256"] or current.byte_size != document["byte_size"]:
        raise SnapshotChanged("原件已变化，请重新选择。")
    return current


def _copy_original(document, store, target):
    current = _original(document)
    from apps.documents.errors import UploadDomainError

    try:
        with store.open_private(current.original_object_key) as stream:
            return _copy_verified(stream, target, current.byte_size, current.sha256)
    except UploadDomainError as error:
        error.document_id = current.pk
        raise


def _archive(entries, snapshot, store, filename):
    manifest = {
        "schema_version": snapshot["schema_version"], "generated_at": snapshot["generated_at"],
        "document_ids": [item["id"] for item in snapshot["documents"]], "files": [],
        "self_record_ids": [item['id'] for item in snapshot.get('self_records', [])],
        "glucose_record_ids": [item['id'] for item in snapshot.get('glucose_records', [])],
        "cloud_source_ids": [item['id'] for item in snapshot.get('cloud_imaging_sources', [])],
        "treatment_event_ids": [item['id'] for item in snapshot.get('treatment_events', [])],
        "cycle_ids": [item['id'] for item in snapshot.get('treatment_cycles', [])],
        "personal_change_ids": [item['id'] for item in snapshot.get('personal_changes', [])],
    }
    output = private_temporary_file()
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for path, payload, metadata in entries:
                if metadata["kind"] == "original":
                    with bundle.open(path, "w", force_zip64=True) as member:
                        size, sha256 = _copy_original(payload, store, member)
                else:
                    bundle.writestr(path, payload)
                    size, sha256 = len(payload), hashlib.sha256(payload).hexdigest()
                manifest["files"].append({"path": path, "byte_size": size, "sha256": sha256, **metadata})
            bundle.writestr("manifest.json", (_json(manifest) + "\n").encode("utf-8"))
        return Artifact.from_stream(output, filename, "application/zip")
    except BaseException:
        output.close()
        raise


def _filename(document):
    # Stable UUID disambiguation also handles duplicate, path-like and reserved names.
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", document["filename"]).strip(" .") or "original"
    if len(name) > 140:
        suffix = PurePosixPath(name).suffix[:12]
        name = name[:120] + suffix
    return "originals/" + document["id"] + "-" + name


def build_artifact(snapshot, options, store):
    from .pdf import render_pdf
    from apps.cloud_imaging.projection import assert_safe_snapshot

    # Original-only output still consumes selected filenames and ZIP metadata.
    # Preserve the selected original bytes, but do not bypass the snapshot rule.
    assert_safe_snapshot(snapshot)
    options = validate_options(options, snapshot)
    kind, parts = options["format"], options["parts"]
    if kind == "original":
        document = snapshot["documents"][0]
        output = private_temporary_file()
        try:
            size, sha256 = _copy_original(document, store, output)
            return Artifact.from_stream(output, document["filename"], document["content_type"], byte_size=size, sha256=sha256)
        except BaseException:
            output.close()
            raise
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
            entries.append((_filename(document), document, {
                "kind": "original", "document_id": document["id"], "original_filename": document["filename"],
            }))
    if not entries:
        raise ExportInputError("没有可生成的内容。")
    return _archive(entries, snapshot, store, "records-csv.zip" if kind == "csv" else "records.zip")
