"""Frozen patient-owned content, shared by the screen, PDF and structured formats."""

from copy import deepcopy
import json
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.models import Document, UploadBatch
from apps.facts.readmodels import digest, review_facts
from apps.labs.comparison import comparable_cell
from apps.labs.models import LabObservation
from apps.labs.readmodels import effective_rows
from apps.labs.trends import _series_for_code
from apps.labs.validation import numeric_value, parse_reference_range, VALIDATION_RULE_VERSION
from apps.patients.models import Patient
from apps.processing.models import SourceEvidence
from apps.self_records.exporting import assert_records_current, record_fingerprint, selected_material

from .errors import ExportInputError, SnapshotChanged
from .selection import identifiers, select_documents


SCHEMA_VERSION = "1.2"
SECTIONS = (("patient", "患者信息"), ("diagnosis", "诊断与分期"), ("treatment", "治疗时间线"),
            ("labs", "重点检验"), ("imaging", "影像与病理"), ("self_records", "日常记录"), ("sources", "来源信息"))
SUSPECT_ISSUES = frozenset({
    "recognition_uncertain", "association_conflict", "normalization_uncertain", "magnitude_suspect",
    "reported_error", "revision_conflict", "source_unavailable", "type_conflict", "source_policy_unknown",
    "numeric_unsupported", "internal_conflict",
})


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def lock_sources(patient, document_ids):
    """Serialize reads with upload, source lifecycle, edits and parse publication."""
    locked_patient = Patient.objects.select_for_update().filter(pk=patient.pk, account__is_active=True, deleted_at__isnull=True).first()
    if locked_patient is None:
        raise PermissionDenied
    ids = identifiers(document_ids)
    if not ids:
        return ()
    # Other normal records can affect the existing historical quality rules.
    # Lock that context too so one snapshot never straddles a concurrent edit.
    query = Document.objects.filter(patient=patient).filter(Q(pk__in=ids) | Q(deleted_at__isnull=True))
    batches = tuple(UploadBatch.objects.select_for_update().filter(
        pk__in=query.values_list("batch_id", flat=True),
    ).order_by("pk"))
    context = tuple(query.select_for_update().order_by("pk"))
    documents = tuple(document for document in context if str(document.pk) in ids)
    if len(documents) != len(ids) or any(document.deleted_at is not None for document in documents):
        raise SnapshotChanged("来源已变化或不可用，请重新选择并生成。")
    return documents


def _lab_date(row):
    if row.date_verified:
        return {"value": row.observation_date.isoformat() if row.observation_date else None,
                "precision": "DAY" if row.observation_date else "UNKNOWN", "raw": row.observation_date.isoformat() if row.observation_date else ""}
    source_id = row.value_sources.get("observation_date", {}).get("observation_id")
    original = LabObservation.objects.select_related("parsing_version").filter(
        pk=source_id, parsing_version__document_id=row.parsing_version.document_id,
    ).first() if source_id else row
    original = original or row
    candidates = original.parsing_version.metadata_candidates.filter(kind="DOCUMENT_DATE", selected=True)
    page = original.field_evidence.get("observation_date", {}).get("page_number")
    if page:
        candidates = candidates.filter(evidence__document_page__page_number=page)
    candidates = list(candidates.order_by("pk"))
    precision = candidates[0].precision if candidates and len({item.precision for item in candidates}) == 1 else "UNKNOWN"
    value = row.observation_date.isoformat() if row.observation_date else None
    if value and precision == "MONTH":
        value = value[:7]
    elif value and precision == "YEAR":
        value = value[:4]
    return {"value": value, "precision": precision if value else "UNKNOWN",
            "raw": "\n".join(item.raw_text for item in candidates)}


def _lab_record(row, previous):
    cell = comparable_cell(row, previous=previous)
    original = LabObservation.objects.get(pk=row.pk)
    comparator = re.match(r"\s*(<=|>=|[<>≤≥])", row.raw_value)
    number = numeric_value(row.raw_value) if row.result_type == "NUMERIC" else None
    return _plain({
        "id": str(row.pk), "document_id": str(row.parsing_version.document_id),
        "parsing_version": str(row.parsing_version_id), "page": row.document_page.page_number,
        "evidence_id": str(row.evidence_id), "field_sources": row.value_sources, "field_evidence": row.field_evidence,
        "raw_name": original.raw_name, "name": row.raw_name, "standard_code": row.standard_code, "standard_name": row.standard_name,
        "raw_value": original.raw_value, "value": row.raw_value, "raw_result_type": original.result_type,
        "result_type": row.result_type, "comparator": comparator.group(1) if comparator else None,
        "numeric_value": str(number) if number is not None else None,
        "raw_unit": original.raw_unit, "unit": row.raw_unit, "date": _lab_date(row),
        "institution": row.institution_raw, "specimen": row.specimen, "method": row.method_raw,
        "reference_range_raw": row.reference_range_raw, "reference_range": parse_reference_range(row.reference_range_raw),
        "reference_definition": row.reference_range, "reference_label": cell.reference_label,
        "raw_report_flag": row.report_flag_raw, "quality_issues": list(cell.quality_issues),
        "review_state": row.review_state, "value_origin": row.value_origin, "capability_level": row.capability_level,
        "revision_number": row.revision_number, "revision_id": str(row.applied_revision.pk) if row.applied_revision else None,
        "dictionary_version": row.dictionary_version, "mapping_dictionary_version": row.mapping_dictionary_version,
        "quality_rule_version": VALIDATION_RULE_VERSION, "normalization_candidates": row.normalization_candidates,
        "comparison": {"state": cell.comparability, "label": cell.comparability_label, "trend_eligible": cell.trend_eligible,
                       "group_key": list(cell.group_key), "unit": cell.unit, "rule": cell.rule,
                       "numeric_value": str(cell.numeric_value) if cell.numeric_value is not None else None},
        "card_eligible": not bool({item["code"] for item in cell.quality_issues} & SUSPECT_ISSUES),
    })


def _source_records(labs, facts, document_ids):
    ids = {item["evidence_id"] for item in labs}
    for item in labs:
        ids.update(source["evidence_id"] for source in item["field_sources"].values() if source.get("evidence_id"))
    output = []
    for evidence in SourceEvidence.objects.filter(
        pk__in=ids, parsing_version__document_id__in=document_ids,
    ).select_related("document_page", "parsing_version").order_by("pk"):
        output.append(_plain({
            "id": str(evidence.pk), "document_id": str(evidence.parsing_version.document_id),
            "page": evidence.document_page.page_number, "parsing_version": str(evidence.parsing_version_id),
            "raw_text": evidence.source_text, "polygon": evidence.polygon,
            "location": "REGION" if evidence.polygon else "PAGE", "confidence": evidence.confidence,
        }))
    for fact in facts:
        output.append({key: value for key, value in fact["source"].items() if key != "url"} | {"id": "fact:" + fact["id"]})
    return output


def _material(patient, selected):
    if not selected:
        return [], [], [], [], []
    rows = effective_rows(patient, include_uncertain=True)
    manifest = select_documents(patient, {"mode": "documents", "document_ids": selected}, rows=rows)
    facts = list(review_facts(patient, document_ids=selected, include_history=True))
    observations = [row for row in rows if str(row.parsing_version.document_id) in selected]
    # Quality context is identical to the existing comparison page, even when a
    # historical reference document is outside the chosen export range.
    labs = [_lab_record(row, rows) for row in observations]
    sources = _source_records(labs, facts, selected)
    return manifest["documents"], facts, observations, labs, sources


def _dependency_fingerprint(documents, facts, labs, sources, clinical=None):
    return digest({"documents": documents, "facts": facts, "labs": labs, "sources": sources,
                   "clinical": clinical or [], "schema": SCHEMA_VERSION})


def _reliable_day(row):
    return bool(row["date"]["value"] and row["date"]["precision"] == "DAY"
                and not {"date_uncertain", "date_conflict"} & {item["code"] for item in row["quality_issues"]})


def _card(selection, documents, facts, observations, labs):
    sections = selection.get("sections", [key for key, _ in SECTIONS])
    if not isinstance(sections, list) or set(sections) - {key for key, _ in SECTIONS}:
        raise ExportInputError("速查卡内容选择无效。")
    eligible = [row for row in labs if row["card_eligible"]]
    if selection.get("lab_codes") is not None:
        codes = selection["lab_codes"]
        if not isinstance(codes, list) or set(codes) - {row["standard_code"] for row in labs}:
            raise ExportInputError("所选检验指标已变化。")
        eligible = [row for row in eligible if row["standard_code"] in codes]
    if selection.get("lab_ids") is not None:
        chosen = identifiers(selection["lab_ids"])
        if set(chosen) - {row["id"] for row in labs}:
            raise ExportInputError("所选检验结果已变化。")
        displayed = [row for row in eligible if row["id"] in chosen]
    else:
        latest = {}
        for row in eligible:
            if _reliable_day(row):
                code = row["standard_code"]
                latest[code] = max(latest.get(code, ""), row["date"]["value"])
        # Multiple samples on the latest day remain separate. Uncertain dates are not sorted as exact dates.
        displayed = [row for row in eligible if not _reliable_day(row)
                     or row["date"]["value"] == latest.get(row["standard_code"])]
    included_codes = {row["standard_code"] for row in displayed}
    trends = []
    exact_ids = {row["id"] for row in labs if row["date"]["precision"] == "DAY" and row["comparison"]["trend_eligible"]}
    for code in sorted(included_codes):
        source_rows = [row for row in observations if row.standard_code == code and str(row.pk) in exact_ids]
        for series in _series_for_code([(row, None) for row in source_rows], previous=observations):
            trends.append({
                "standard_code": code, "standard_name": source_rows[0].standard_name or source_rows[0].raw_name,
                "unit": series.unit, "basis": series.basis_label,
                "points": [{"id": str(point.observation.pk), "date": point.observation.observation_date.isoformat(),
                            "value": str(point.numeric_value), "rule": _plain(point.conversion_rule)} for point in series.points],
            })
    groups = {
        "diagnosis": [row for row in facts if row["category"] in {"DIAGNOSIS", "STAGE"}],
        "treatment": sorted([row for row in facts if row["category"] == "TREATMENT"], key=lambda row: (row["content"]["date"] is None, row["content"]["date"] or "", row["id"])),
        "imaging": [row for row in facts if row["category"] in {"IMAGING", "PATHOLOGY"}],
    }
    return {
        "sections": [{"key": key, "title": title, "included": key in sections} for key, title in SECTIONS
                     if key != "self_records" or selection.get("self_record_ids")],
        "groups": groups, "lab_ids": [row["id"] for row in displayed] if "labs" in sections else [],
        "trends": trends if "labs" in sections else [], "details": selection.get("details", False),
        "self_record_ids": selection.get("self_record_ids", []) if "self_records" in sections else [],
    }


def build_snapshot(patient, selection, *, now=None):
    if not isinstance(selection, dict):
        raise ExportInputError("导出选择无效。")
    selection = deepcopy(selection)
    with transaction.atomic():
        if Patient.objects.select_for_update().filter(pk=patient.pk, account__is_active=True, deleted_at__isnull=True).first() is None:
            raise PermissionDenied
        manifest = select_documents(patient, selection)
        ids = [item["id"] for item in manifest["documents"]]
        lock_sources(patient, ids)
        self_records = selected_material(patient, selection, lock=True)
        if not ids and not self_records:
            raise ExportInputError("请至少选择一份正常资料或一条日常记录；不会生成空资料包。")
        documents, all_facts, observations, labs, sources = _material(patient, ids)
        from apps.facts.clinical_readmodels import report_material
        from .clinical import clinical_projection
        clinical = report_material(patient, document_ids=ids, include_history=True)
        clinical_selected = clinical_projection(clinical, selection)
        dependency = _dependency_fingerprint(documents, all_facts, labs, sources, clinical)
        fine_clinical_scope = selection.get("report_ids") is not None or selection.get("clinical_field_ids") is not None
        if fine_clinical_scope:
            for key in ("fact_ids", "observation_ids"):
                if selection.get(key) is None:
                    selection[key] = []
        facts = [row for row in all_facts if row["usable"]]
        if selection.get("fact_ids") is not None:
            chosen = identifiers(selection["fact_ids"])
            if set(chosen) - {row["id"] for row in facts}:
                raise ExportInputError("部分选定事实尚未核对或已失效，请重新确认。")
            facts = [row for row in facts if row["id"] in chosen]
        if selection.get("observation_ids") is not None:
            chosen = identifiers(selection["observation_ids"])
            if set(chosen) - {row["id"] for row in labs}:
                raise ExportInputError("部分选定检验结果已失效或不属于所选资料。")
            labs = [row for row in labs if row["id"] in chosen]
            observations = [row for row in observations if str(row.pk) in chosen]
        sources = _source_records(labs, facts, ids)
        nickname = selection.get("nickname", patient.display_name)
        basic_info = selection.get("basic_info", "")
        if not isinstance(nickname, str) or not isinstance(basic_info, str):
            raise ExportInputError("姓名和基本信息必须为文字。")
        nickname, basic_info = nickname.strip(), basic_info.strip()
        if not nickname or len(nickname) > 80 or len(basic_info) > 500 or type(selection.get("details", False)) is not bool:
            raise ExportInputError("请填写姓名或昵称；基本信息可留空。")
        selection.update(document_ids=ids, nickname=nickname, basic_info=basic_info,
                         self_record_ids=[row["id"] for row in self_records])
        card = _card(selection, documents, [*facts, *clinical_selected["clinical_fields"]], observations, labs)
        used_fact_ids = {row["id"] for row in facts}
        return {
            "schema_version": SCHEMA_VERSION, "patient_id": str(patient.pk),
            **clinical_selected,
            "original_scope_warning": bool(selection.get("report_ids") is not None or selection.get("clinical_field_ids") is not None),
            "generated_at": timezone.localtime(now or timezone.now()).isoformat(),
            "selection": selection, "patient": {"nickname": nickname, "basic_info": basic_info},
            "documents": documents,
            "self_records": self_records, "self_record_fingerprint": record_fingerprint(self_records),
            "facts": [{key: deepcopy(row[key]) for key in (
                "id", "origin", "category", "category_label", "content", "status", "revision_number", "revision_id", "source",
            )} for row in facts],
            "labs": labs, "sources": [row for row in sources if not row["id"].startswith("fact:")
                                       or row["id"][5:] in used_fact_ids], "card": card,
            "excluded_documents": manifest["excluded"], "uncertain_documents": manifest["uncertain"],
            "excluded_facts": [{"id": row["id"], "document_id": row["source"]["document_id"],
                                "category_label": row["category_label"], "page": row["source"]["page"],
                                "reason": row["reason"] or "未选择纳入"} for row in all_facts if row["id"] not in used_fact_ids],
            "excluded_card_labs": [{"id": row["id"], "name": row["standard_name"],
                                   "reason": "存在疑似识别问题" if not row["card_eligible"] else "未选择或不是最近可用结果"}
                                  for row in labs if row["id"] not in card["lab_ids"]],
            "dependency_fingerprint": dependency,
        }


def assert_snapshot_current(patient, snapshot):
    if not snapshot or snapshot.get("patient_id") != str(patient.pk):
        raise PermissionDenied
    with transaction.atomic():
        ids = [item["id"] for item in snapshot["documents"]]
        lock_sources(patient, ids)
        assert_records_current(patient, snapshot)
        documents, facts, _rows, labs, sources = _material(patient, ids)
        from apps.facts.clinical_readmodels import report_material
        clinical = report_material(patient, document_ids=ids, include_history=True)
        if snapshot["dependency_fingerprint"] != _dependency_fingerprint(documents, facts, labs, sources, clinical):
            raise SnapshotChanged("资料、核对状态或版本已变化，请重新确认清单并生成。")
