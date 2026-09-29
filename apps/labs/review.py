"""Retired delegated review service; historical tasks remain for audit."""

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .models import ReviewTask, ReviewTaskEvent, ReviewTaskStatus


def _retired():
    raise PermissionDenied('授权复核功能已停用。')


def get_review_task(actor, task_id):
    _retired()


def create_review_task(owner, observation_id, *, reviewer=None):
    _retired()


def assign_review_task(owner, task_id, *, reviewer, expected_revision):
    _retired()


def transition_review_task(actor, task_id, *, action, expected_revision, changes=None, resolved_issues=()):
    _retired()


def revoke_document_reviews(document, *, actor=None, action='DOCUMENT_DELETED'):
    """Preserve prior task audit when the underlying document is deleted."""
    for task in ReviewTask.objects.select_for_update().filter(
        observation__parsing_version__document=document, revoked_at__isnull=True,
    ).order_by('pk'):
        before = task.status
        task.status = ReviewTaskStatus.REVOKED
        task.revoked_at = timezone.now()
        task.revision_number += 1
        task.save(update_fields=['status', 'revoked_at', 'revision_number', 'updated_at'])
        ReviewTaskEvent.objects.create(
            task=task, author=actor, sequence=task.revision_number, action=action,
            before_status=before, after_status=task.status,
        )
