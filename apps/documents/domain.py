from django.db import transaction

from .models import Document, DocumentStatus


class InvalidTransition(ValueError):
    pass


_ALLOWED_TARGETS = {
    DocumentStatus.PROCESSING: {
        DocumentStatus.ORGANIZED,
        DocumentStatus.ORIGINAL_ONLY,
        DocumentStatus.PROCESSING_FAILED,
    },
    DocumentStatus.PROCESSING_FAILED: {DocumentStatus.PROCESSING},
}


def transition_document(document, target):
    """Atomically apply the only public document-status transitions."""
    try:
        target_status = DocumentStatus(target)
    except ValueError as error:
        raise InvalidTransition("Unknown document transition target") from error

    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if locked.deleted_at is not None:
            raise InvalidTransition("Soft-deleted documents cannot transition")
        current = DocumentStatus(locked.status)
        if current == target_status:
            return locked
        if target_status not in _ALLOWED_TARGETS.get(current, set()):
            raise InvalidTransition("Document transition is not allowed")
        locked.status = target_status
        locked.save(update_fields=["status", "updated_at"])
        return locked
