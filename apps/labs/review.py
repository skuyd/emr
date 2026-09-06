"""Task-scoped authorization, state transitions, and transcription review."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.operations.permissions import current_actor

from .models import ReviewTask, ReviewTaskEvent, ReviewTaskStatus
from .revisions import RevisionConflict, append_revision, lock_observation


def _is_reviewer(actor):
    return bool(actor.is_active and actor.is_staff and actor.has_perm("labs.review_labobservation"))


def _authorize_task(actor, task, *, allow_owner=True):
    document = task.observation.parsing_version.document
    if not actor.is_active or document.deleted_at is not None or not document.patient.account.is_active:
        raise PermissionDenied
    if allow_owner and document.patient.account_id == actor.pk:
        return
    if (not _is_reviewer(actor) or task.reviewer_id != actor.pk or task.revoked_at is not None
            or task.status == ReviewTaskStatus.REVOKED or task.expires_at <= timezone.now()):
        raise PermissionDenied
    if not task.observation.parsing_version.active:
        raise RevisionConflict("任务对应的解析版本已过期。")


def get_review_task(actor, task_id):
    task = ReviewTask.objects.select_related(
        "observation__parsing_version__document__patient__account", "observation__evidence",
        "observation__document_page", "reviewer",
    ).filter(pk=task_id).first()
    if task is None:
        raise PermissionDenied
    _authorize_task(actor, task)
    return task


def create_review_task(owner, observation_id, *, reviewer=None):
    with transaction.atomic():
        document, observation = lock_observation(observation_id)
        owner = current_actor(owner)
        if not owner.is_active or document.patient.account_id != owner.pk:
            raise PermissionDenied
        if not observation.parsing_version.active:
            raise RevisionConflict("只能复核当前解析版本。")
        if reviewer is not None:
            reviewer = current_actor(reviewer)
            if not _is_reviewer(reviewer):
                raise ValidationError("只能授权具备复核权限的人员。")
        task = ReviewTask.objects.create(
            observation=observation, reviewer=reviewer, granted_by=owner,
            observation_revision=observation.revision_number, expires_at=timezone.now() + timedelta(days=7),
        )
        ReviewTaskEvent.objects.create(
            task=task, author=owner, sequence=0, action="CREATE", after_status=task.status,
        )
        return task


def assign_review_task(owner, task_id, *, reviewer, expected_revision):
    with transaction.atomic():
        identity = ReviewTask.objects.filter(pk=task_id).values_list("observation_id", flat=True).first()
        document, _row = lock_observation(identity)
        task = ReviewTask.objects.select_for_update().get(pk=task_id)
        owner = current_actor(owner)
        if not owner.is_active or document.patient.account_id != owner.pk:
            raise PermissionDenied
        if task.revision_number != expected_revision or not task.observation.parsing_version.active:
            raise RevisionConflict("任务已更新。")
        reviewer = current_actor(reviewer)
        if task.status != ReviewTaskStatus.PENDING or task.revoked_at or not _is_reviewer(reviewer):
            raise ValidationError("当前任务无法分配。")
        task.reviewer = reviewer
        task.expires_at = timezone.now() + timedelta(days=7)
        task.revision_number += 1
        task.save(update_fields=["reviewer", "expires_at", "revision_number", "updated_at"])
        ReviewTaskEvent.objects.create(
            task=task, author=owner, sequence=task.revision_number,
            action="ASSIGN", before_status=task.status, after_status=task.status,
        )
        return task


def transition_review_task(actor, task_id, *, action, expected_revision, changes=None, resolved_issues=()):
    with transaction.atomic():
        identity = ReviewTask.objects.filter(pk=task_id).values_list("observation_id", flat=True).first()
        document, observation = lock_observation(identity)
        task = ReviewTask.objects.select_for_update().get(pk=task_id)
        actor = current_actor(actor)
        document.patient.account = current_actor(document.patient.account)
        task.observation = observation
        observation.parsing_version.document = document
        _authorize_task(actor, task, allow_owner=action == "REVOKE")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or task.revision_number != expected_revision:
            raise RevisionConflict("任务已更新，请刷新后重试。")
        if action == "REVOKE":
            if document.patient.account_id != actor.pk:
                raise PermissionDenied
            if task.status == ReviewTaskStatus.REVOKED:
                raise ValidationError("授权已经撤回。")
            target = ReviewTaskStatus.REVOKED
        else:
            if not observation.parsing_version.active or observation.revision_number != task.observation_revision:
                raise RevisionConflict("资料或修订版本已更新。")
            allowed = {
                (ReviewTaskStatus.PENDING, "START"): ReviewTaskStatus.IN_PROGRESS,
                (ReviewTaskStatus.IN_PROGRESS, "CONFIRM"): ReviewTaskStatus.COMPLETED,
                (ReviewTaskStatus.IN_PROGRESS, "CORRECT"): ReviewTaskStatus.COMPLETED,
                (ReviewTaskStatus.IN_PROGRESS, "UNABLE"): ReviewTaskStatus.UNABLE,
            }
            target = allowed.get((task.status, action))
            if target is None:
                raise ValidationError("当前状态不能执行该操作。")
        revision = None
        if action in {"CONFIRM", "CORRECT"}:
            revision = append_revision(
                actor, observation, action=action, changes=changes or {},
                expected_revision=task.observation_revision, origin="REVIEW", resolved_issues=resolved_issues,
            )
        elif changes or resolved_issues:
            raise ValidationError("该状态变化不能修改字段。")
        before = task.status
        task.status = target
        task.revision_number += 1
        if target == ReviewTaskStatus.REVOKED:
            task.revoked_at = timezone.now()
        task.save(update_fields=["status", "revision_number", "revoked_at", "updated_at"])
        ReviewTaskEvent.objects.create(
            task=task, author=actor, sequence=task.revision_number, action=action,
            before_status=before, after_status=target, revision=revision,
        )
        return task


def revoke_document_reviews(document, *, actor=None, action="DOCUMENT_DELETED"):
    """Called under the existing deletion aggregate lock, before any source is removed."""
    for task in ReviewTask.objects.select_for_update().filter(
        observation__parsing_version__document=document, revoked_at__isnull=True,
    ).order_by("pk"):
        before = task.status
        task.status = ReviewTaskStatus.REVOKED
        task.revoked_at = timezone.now()
        task.revision_number += 1
        task.save(update_fields=["status", "revoked_at", "revision_number", "updated_at"])
        ReviewTaskEvent.objects.create(
            task=task, author=actor, sequence=task.revision_number, action=action,
            before_status=before, after_status=task.status,
        )
