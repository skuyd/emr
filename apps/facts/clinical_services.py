"""Authorized report boundaries, manual fields and auditable aggregate actions."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.exports.services import invalidate_document_exports
from apps.operations.audit import record_audit_event
from apps.patients.access import Capability, authorize_patient
from apps.processing.models import OcrBlock

from .clinical_readmodels import report_queryset, report_source_token, report_state
from .clinical_schema import SCHEMA_VERSION, field_content
from .models import ClinicalExtraction, ClinicalReport, ClinicalReportRevision, ClinicalReportSpan, Fact, FactSourceFragment
from .readmodels import digest, effective_fact
from .revisions import FactConflict, revise_fact


def _document(access, document_id):
    document, _ = lock_document_aggregate(document_id, patient_id=access.patient.pk)
    if document is None or document.deleted_at is not None:
        raise PermissionDenied
    return document


def _report(access, report_id):
    identity = ClinicalReport.objects.filter(pk=report_id, document__patient=access.patient).values_list("document_id", flat=True).first()
    if identity is None:
        raise PermissionDenied
    _document(access, identity)
    return report_queryset().get(pk=report_id)


def request_clinical_extraction(patient, *, actor, document_id, expected_version):
    from .clinical_extraction import extract_clinical_version

    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        document = _document(access, document_id)
        version = document.parsing_versions.filter(active=True).first()
        if version is None or str(version.pk) != str(expected_version):
            raise FactConflict("当前识别版本已变化；无OCR时请按原件页人工建立报告。")
        ClinicalExtraction.objects.filter(parsing_version=version, status="FAILED").delete()
        result = extract_clinical_version(version)
        invalidate_document_exports(document)
        record_audit_event(access.actor.pk, "clinical_extraction_requested", document.pk, "succeeded")
        return result


def create_manual_report(patient, *, actor, document_id, spans, title, expected_lifecycle_revision, expected_version_id):
    if not isinstance(spans, list) or not spans or len(spans) > 1000 or not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValidationError("请填写报告名称并明确至少一个原件页或片段范围。")
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        document = _document(access, document_id)
        version = document.parsing_versions.filter(active=True).first()
        if (type(expected_lifecycle_revision) is not int or expected_lifecycle_revision != document.lifecycle_revision
                or str(expected_version_id) != str(version.pk if version else None)):
            raise FactConflict("资料或识别版本已变化，请刷新后重新选择报告范围。")
        report = ClinicalReport(
            document=document, parsing_version=version, origin="MANUAL", routing_kind="IMAGING",
            ordinal=document.clinical_reports.count(), title=title.strip(), segmenter_version="manual-report-v1",
            schema_version=SCHEMA_VERSION, source_fingerprint=digest({"document": str(document.pk), "sha256": document.sha256,
                                                                     "version": str(version.pk if version else None), "spans": spans}),
            lifecycle_revision=document.lifecycle_revision, created_by=access.actor,
        )
        report.full_clean()
        report.save()
        seen = set()
        for ordinal, values in enumerate(spans):
            if (not isinstance(values, dict) or set(values) - {"page_number", "ocr_block_id", "start_offset", "end_offset"}
                    or type(values.get("page_number")) is not int):
                raise ValidationError("原件页范围无效。")
            page = document.pages.filter(page_number=values["page_number"]).first()
            if page is None:
                raise ValidationError("报告范围包含其他原件或不存在的页。")
            block = None
            start, end, raw = None, None, ""
            if values.get("ocr_block_id"):
                block = OcrBlock.objects.filter(pk=values["ocr_block_id"], parsing_version=version, document_page=page).first()
                if block is None:
                    raise ValidationError("所选文字片段不属于当前原件页与识别版本。")
                start, end = values.get("start_offset"), values.get("end_offset")
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block.text):
                    raise ValidationError("文字片段范围已变化。")
                raw = block.text[start:end]
            elif any(values.get(key) is not None for key in ("start_offset", "end_offset")):
                raise ValidationError("无OCR片段不能指定字符偏移。")
            identity = (page.pk, block.pk if block else None, start, end)
            if identity in seen:
                raise ValidationError("报告范围不能重复。")
            seen.add(identity)
            span = ClinicalReportSpan(report=report, document_page=page, ocr_block=block, ordinal=ordinal,
                                      start_offset=start, end_offset=end, raw_text=raw, boundary_basis="MANUAL_EXPLICIT_RANGE")
            span.full_clean()
            span.save()
        invalidate_document_exports(document)
        record_audit_event(access.actor.pk, "clinical_report_added", report.pk, "succeeded")
        return report


def add_manual_clinical_field(patient, *, actor, report_id, entity_key, field_key, value, fragments, expected_report_source):
    if not isinstance(fragments, list) or not fragments or len(fragments) > 100:
        raise ValidationError("请注明字段原文及对应页码。")
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        report = _report(access, report_id)
        state = report_state(report)
        if not state["source_valid"] or state["status"] == "EXCLUDED" or expected_report_source != state["current_source_token"]:
            raise FactConflict("报告范围或来源已变化，请重新核对。")
        validated = []
        for values in fragments:
            if (not isinstance(values, dict) or set(values) != {"page_number", "raw_text"}
                    or type(values["page_number"]) is not int or not isinstance(values["raw_text"], str)
                    or not values["raw_text"].strip() or len(values["raw_text"]) > 30000):
                raise ValidationError("请填写对应原件页中的完整字段原文。")
            span = report.spans.filter(document_page__page_number=values["page_number"]).select_related("document_page").first()
            if span is None:
                raise ValidationError("补录来源页不在报告范围内。")
            validated.append((span.document_page, values["raw_text"].strip()))
        raw_text = "\n".join(text for _, text in validated)
        fact = Fact(document=report.document, document_page=validated[0][0], parsing_version=report.parsing_version,
                    origin="MANUAL", category="IMAGING", representation="FIELD", clinical_report=report,
                    field_key=field_key, entity_key=entity_key, schema_version=SCHEMA_VERSION,
                    raw_text=raw_text, automatic_content=field_content(field_key, value, raw_text),
                    reading_order=report.fields.count(), created_by=access.actor)
        fact.full_clean()
        fact.save()
        for ordinal, (page, text) in enumerate(validated):
            fragment = FactSourceFragment(fact=fact, ordinal=ordinal, document_page=page, source_kind="MANUAL", raw_text=text)
            fragment.full_clean()
            fragment.save()
        invalidate_document_exports(report.document)
        record_audit_event(access.actor.pk, "clinical_field_added", fact.pk, "succeeded")
        return fact


def revise_report(patient, *, actor, report_id, action, expected_revision, expected_source):
    if action not in {"EXCLUDE", "UNDO"}:
        raise ValidationError("报告范围仅支持整体排除或撤销上次范围操作。")
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        report = _report(access, report_id)
        state = report_state(report)
        if (type(expected_revision) is not int or expected_revision != report.revision_number
                or expected_source != state["current_source_token"] or not state["source_valid"]):
            raise FactConflict("报告或来源已变化，请刷新后核对。")
        fields = list(report.fields.order_by("pk"))
        before = {"status": state["status"]}
        entries = []
        if action == "UNDO":
            latest = report.revisions.order_by("-sequence").first()
            if latest is None:
                raise ValidationError("本报告没有可撤销的范围操作。")
            if latest.action in {"UNDO", "REPLACEMENT_UNDONE"}:
                raise ValidationError("该范围操作已撤销；请重新选择报告范围，不能恢复已被替换的关联。")
            expected = {entry["fact_id"]: entry for entry in latest.field_revisions}
            if set(expected) != {str(fact.pk) for fact in fields} or any(
                fact.revision_number != expected[str(fact.pk)]["sequence"]
                or str(fact.revisions.order_by("-sequence").first().pk) != expected[str(fact.pk)]["revision_id"] for fact in fields
            ):
                raise FactConflict("报告中已有单独字段修改，不能整批撤销覆盖，请逐项核对。")
            if latest.action == "REPLACE":
                replacement = _report(access, latest.after["replacement_id"])
                replacement_fields = list(replacement.fields.order_by("pk"))
                if (replacement.revision_number != 0 or report_state(replacement)["status"] != "ACTIVE"
                        or {str(f.pk) for f in replacement_fields} != set(latest.after["replacement_field_ids"])
                        or any(f.revision_number != 0 for f in replacement_fields)):
                    raise FactConflict("替换后的报告已有核对或补录，不能整体撤销覆盖，请逐项核对。")
                replacement_entries = _exclude_fields(access, replacement)
                _record_report_action(access, replacement, "REPLACEMENT_UNDONE", {"status": "ACTIVE"},
                                      {"status": "EXCLUDED", "restored_report_id": str(report.pk)}, replacement_entries)
            after = latest.before
        else:
            after = {"status": "EXCLUDED"}
        for fact in fields:
            revision = revise_fact(access.patient, fact.pk, actor=access.actor, action="UNDO" if action == "UNDO" else "EXCLUDE",
                                   expected_revision=fact.revision_number, expected_source=effective_fact(fact)["current_source_token"])
            entries.append({"fact_id": str(fact.pk), "revision_id": str(revision.pk), "sequence": revision.sequence})
        event = ClinicalReportRevision.objects.create(report=report, author=access.actor, sequence=report.revision_number + 1,
                                                      action=action, before=before, after=after, field_revisions=entries,
                                                      source_token=state["current_source_token"])
        report.revision_number += 1
        report.save(update_fields=["revision_number"])
        invalidate_document_exports(report.document)
        record_audit_event(access.actor.pk, "clinical_report_revised", report.pk, "succeeded", action.lower())
        return event


def _exclude_fields(access, report):
    entries = []
    for fact in report.fields.order_by("pk"):
        revision = revise_fact(access.patient, fact.pk, actor=access.actor, action="EXCLUDE",
                               expected_revision=fact.revision_number, expected_source=effective_fact(fact)["current_source_token"])
        entries.append({"fact_id": str(fact.pk), "revision_id": str(revision.pk), "sequence": revision.sequence})
    return entries


def _record_report_action(access, report, action, before, after, entries):
    event = ClinicalReportRevision.objects.create(report=report, author=access.actor, sequence=report.revision_number + 1,
                                                  action=action, before=before, after=after, field_revisions=entries,
                                                  source_token=report_source_token(report))
    report.revision_number += 1
    report.save(update_fields=["revision_number"])
    invalidate_document_exports(report.document)
    record_audit_event(access.actor.pk, "clinical_report_revised", report.pk, "succeeded", action.lower())
    return event


def replace_report_boundary(patient, *, actor, report_id, title, spans, expected_revision, expected_source):
    from .clinical_extraction import field_candidates, persist_candidates
    from .clinical_segments import Piece, Segment

    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        report = _report(access, report_id)
        state = report_state(report)
        if (type(expected_revision) is not int or expected_revision != report.revision_number
                or expected_source != state["current_source_token"] or not state["source_valid"] or state["status"] != "ACTIVE"):
            raise FactConflict("报告或来源已变化，请刷新后重新选择范围。")
        replacement = create_manual_report(access.patient, actor=access.actor, document_id=report.document_id,
                                           title=title, spans=spans, expected_version_id=report.parsing_version_id,
                                           expected_lifecycle_revision=report.document.lifecycle_revision)
        pieces = [Piece(span.ocr_block, span.start_offset, span.end_offset)
                  for span in replacement.spans.select_related("ocr_block__document_page").order_by("ordinal") if span.ocr_block_id]
        if pieces:
            persist_candidates(replacement, field_candidates(Segment(replacement.title, pieces)))
        entries = _exclude_fields(access, report)
        _record_report_action(access, report, "REPLACE", {"status": state["status"]},
                              {"status": "EXCLUDED", "replacement_id": str(replacement.pk),
                               "replacement_field_ids": [str(pk) for pk in replacement.fields.values_list("pk", flat=True)]}, entries)
        return replacement
