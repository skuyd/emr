from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.operations.audit import record_audit_event
from apps.patients.models import Patient

from .extraction import content_for, explicit_dates
from .models import Fact, FactCategory, FactRevision
from .readmodels import effective_fact, fact_queryset


class FactConflict(ValueError):
    pass


def _lock_document(patient, document_id):
    document, _batches = lock_document_aggregate(document_id, patient_id=patient.pk)
    if document is None or document.deleted_at is not None or not Patient.objects.filter(pk=patient.pk, account__is_active=True).exists():
        raise PermissionDenied
    return document


def add_manual_fact(patient, document_id, *, page_number, category, text):
    if category not in FactCategory.values or not isinstance(text, str) or not text.strip() or len(text) > 30000:
        raise ValidationError("请选择事实类型并填写原件中的完整摘录。")
    with transaction.atomic():
        document = _lock_document(patient, document_id)
        if isinstance(page_number, bool) or not isinstance(page_number, int):
            raise ValidationError("请选择有效页码。")
        page = document.pages.filter(page_number=page_number).first()
        if page is None:
            raise ValidationError("请选择原件中的有效页码。")
        version = document.parsing_versions.filter(active=True).first()
        summary = getattr(version, "document_summary", None) if version else None
        fact = Fact(
            document=document, document_page=page, parsing_version=version, origin="MANUAL",
            category=category, raw_text=text.strip(), automatic_content=content_for(category, text.strip(), summary),
            created_by_id=patient.account_id,
        )
        fact.full_clean()
        fact.save()
        record_audit_event(patient.account_id, "fact_added", fact.pk, "succeeded", "manual")
        return fact


def revise_fact(patient, fact_id, *, action, expected_revision, checked_original=False, changes=None, expected_source=None):
    with transaction.atomic():
        identity = Fact.objects.filter(pk=fact_id).values_list("document_id", flat=True).first()
        _lock_document(patient, identity)
        fact = fact_queryset().get(pk=fact_id)
        # The document lock serializes both reviewers and parse activation.
        before = effective_fact(fact)
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision != fact.revision_number or before["historical"]):
            raise FactConflict("事实或解析版本已更新，请刷新并核对当前来源。")
        if expected_source is not None and expected_source != before["current_source_token"]:
            raise FactConflict("来源已变化，请重新打开原件核对。")
        if action not in {"CONFIRM", "CORRECT", "DEFER", "EXCLUDE", "REVOKE", "UNDO"}:
            raise ValidationError("未知核对操作。")
        if action in {"CONFIRM", "CORRECT"} and checked_original is not True:
            raise ValidationError("请先对照原件核对完整摘录，确认不代表判断报告医学结论。")
        if action in {"CONFIRM", "CORRECT"} and not before["source_valid"]:
            raise FactConflict("来源证据不匹配，请重新提取或对照原件补录。")
        if changes and action != "CORRECT":
            raise ValidationError("请使用更正操作修改内容。")
        prior = {key: deepcopy(before[key]) for key in ("content", "status", "source_token")}
        after = deepcopy(prior)
        if action == "UNDO":
            latest = fact.revisions.order_by("-sequence").first()
            if latest is None:
                raise ValidationError("本条没有可撤销的操作。")
            after = deepcopy(latest.before)
        else:
            after["status"] = {"CONFIRM": "CONFIRMED", "CORRECT": "CONFIRMED", "DEFER": "DEFERRED",
                               "EXCLUDE": "EXCLUDED", "REVOKE": "PENDING"}[action]
            after["source_token"] = before["current_source_token"]
        if action == "CORRECT":
            if not changes or set(changes) - {"category", "text", "date_raw", "record_date_raw", "institution"}:
                raise ValidationError("更正仅支持摘录、治疗日期原文和机构。")
            for key, value in changes.items():
                if not isinstance(value, str) or len(value) > (30000 if key == "text" else 512):
                    raise ValidationError("更正内容超出允许范围。")
                after["content"][key] = value.strip()
            if not after["content"]["text"]:
                raise ValidationError("摘录不能为空。")
            if after["content"]["category"] not in FactCategory.values:
                raise ValidationError("请选择有效事实类型。")
            if "record_date_raw" in changes:
                dates = explicit_dates(changes["record_date_raw"])
                after["content"]["record_date"] = (
                    {"raw": changes["record_date_raw"], "value": dates[0]["value"], "precision": dates[0]["precision"]}
                    if len({item["value"] for item in dates}) == 1 else
                    {"raw": changes["record_date_raw"], "value": None, "precision": "UNKNOWN"} if changes["record_date_raw"] else None
                )
            # Retain only explicitly transcribed dates; unknown values stay unknown.
            if after["content"]["category"] == FactCategory.TREATMENT:
                dates = explicit_dates(changes.get("date_raw", after["content"]["text"]))
                only = dates[0] if len({item["value"] for item in dates}) == 1 else None
                after["content"].update(
                    dates=dates, date=only["value"] if only else None,
                    date_precision=only["precision"] if only else "UNKNOWN",
                    date_raw=changes.get("date_raw", only["raw"] if only else ""),
                    limitations=[
                        flag for flag in after["content"].get("limitations", [])
                        if flag not in {"multiple_explicit_dates", "event_date_unknown"}
                    ] + (["multiple_explicit_dates"] if len({item["value"] for item in dates}) > 1 else [] if dates else ["event_date_unknown"]),
                )
        revision = FactRevision.objects.create(
            fact=fact, author_id=patient.account_id, sequence=fact.revision_number + 1, action=action,
            before=prior, after=after, checked_original=checked_original, source=before["source"],
        )
        fact.revision_number += 1
        fact.save(update_fields=["revision_number"])
        record_audit_event(patient.account_id, "fact_revised", fact.pk, "succeeded", action.lower())
        return revision
