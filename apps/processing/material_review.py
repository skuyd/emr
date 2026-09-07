"""Recover from uncertain material suggestions without uploading another file."""
from copy import deepcopy

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.documents.models import DocumentStatus, MaterialOverride
from apps.operations.audit import record_audit_event
from apps.patients.models import Patient

from .material import DOCUMENT, NON_DOCUMENT, UNCERTAIN
from .models import MaterialDecision
from .reprocessing import queue_user_reprocessing, ReprocessingUnavailable


class MaterialReviewConflict(ValueError):
    pass


_UNSET = object()


def material_state(document, version=_UNSET):
    if version is _UNSET:
        versions = getattr(document, "material_versions", None)
        version = (versions[0] if versions else None) if versions is not None else document.parsing_versions.filter(active=True).first()
    assessment = version.diagnostics.get("material", {}) if version else {}
    if not isinstance(assessment, dict) or assessment.get("source_sha256") != document.sha256:
        assessment = {}
    automatic = assessment.get("status", UNCERTAIN)
    available = automatic in {DOCUMENT, NON_DOCUMENT, UNCERTAIN} and bool(assessment.get("version"))
    automatic = automatic if automatic in {DOCUMENT, NON_DOCUMENT, UNCERTAIN} else UNCERTAIN
    override = document.material_override
    label = ""
    if override == MaterialOverride.KEEP_DOCUMENT:
        label = "已按资料保留"
    elif available and automatic == NON_DOCUMENT:
        label = "可能不是单据"
    elif available and automatic == UNCERTAIN:
        label = "暂无法判断是否为单据"
    elif available and any(page.get("status") == NON_DOCUMENT for page in assessment.get("pages", [])):
        label = "部分页面可能不是单据"
    elif available and any(page.get("status") == UNCERTAIN for page in assessment.get("pages", [])):
        label = "部分页面暂无法判断是否为单据"
    return {
        "status": DOCUMENT if override == MaterialOverride.KEEP_DOCUMENT else automatic,
        "automatic_status": automatic, "override": override, "label": label,
        "assessed": available, "policy_version": assessment.get("version", ""),
        "pages": assessment.get("pages", []), "version_id": str(version.pk) if version else "",
        "revision": document.material_revision,
        "can_keep": available and automatic != DOCUMENT and override == MaterialOverride.AUTO
            and document.status != DocumentStatus.PROCESSING,
    }


def material_projection(document):
    state = material_state(document)
    return {key: state[key] for key in ("status", "automatic_status", "override", "label", "assessed")}


def review_material(patient, document_id, *, actor, action, expected_version, expected_revision, dispatch):
    if (action not in MaterialOverride.values or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int) or expected_revision < 0):
        raise MaterialReviewConflict("选择已变化，请刷新后重试。")
    with transaction.atomic():
        # This branch starts from the current owner-only main contract. The same
        # patient-first guard is retained when integrating family authorization.
        locked_patient = Patient.objects.select_for_update().filter(
            pk=patient.pk, account_id=getattr(actor, "pk", actor), account__is_active=True,
        ).first()
        if locked_patient is None:
            raise PermissionDenied
        document, _batches = lock_document_aggregate(document_id, patient_id=locked_patient.pk)
        if document is None or document.deleted_at is not None:
            raise PermissionDenied
        current = document.material_decisions.order_by("-sequence").first()
        if document.material_revision != expected_revision:
            if (current and current.sequence == expected_revision + 1 and current.action == action
                    and str(current.parsing_version_id) == str(expected_version)
                    and current.author_id == getattr(actor, "pk", actor)):
                return current
            raise MaterialReviewConflict("保留方式已更新，请刷新后重试。")
        version = document.parsing_versions.filter(active=True).first()
        if version is None or str(version.pk) != str(expected_version):
            raise MaterialReviewConflict("识别版本已变化，请刷新后重新核对。")
        state = material_state(document, version)
        if action == MaterialOverride.KEEP_DOCUMENT and not state["can_keep"]:
            raise MaterialReviewConflict("当前资料无需重复确认，请刷新查看状态。")
        if action == MaterialOverride.AUTO and document.material_override != MaterialOverride.KEEP_DOCUMENT:
            raise MaterialReviewConflict("当前已经使用自动判断。")
        document.material_revision += 1
        document.material_override = action
        document.save(update_fields=["material_revision", "material_override", "updated_at"])
        run = None
        if action == MaterialOverride.KEEP_DOCUMENT:
            try:
                run = queue_user_reprocessing(locked_patient, document.pk, actor=actor, dispatch=dispatch, for_material_review=True)
            except ReprocessingUnavailable:
                raise MaterialReviewConflict("资料正在整理或暂时无法重试，请稍后刷新。") from None
        decision = MaterialDecision.objects.create(
            document=document, parsing_version=version, processing_run=run,
            author_id=getattr(actor, "pk", actor), sequence=document.material_revision, action=action,
            source_sha256=document.sha256, automatic_snapshot=deepcopy(version.diagnostics.get("material", {})),
        )
        record_audit_event(getattr(actor, "pk", actor), "document_material_reviewed", document.pk, "succeeded", action.lower())
        return decision
