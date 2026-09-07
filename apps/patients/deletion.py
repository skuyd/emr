"""Durable patient deletion; object identities survive until all cleanup succeeds."""

from django.db import transaction
from django.utils import timezone

from .access import Capability, authorize_patient
from .models import Patient, PatientDeletionJob, PatientMembership


def request_patient_deletion(patient_id, actor, *, document_dispatch, now=None):
    from apps.documents.deletion import request_document_deletion
    from apps.documents.models import Document, DocumentDeletionJob
    from apps.exports.services import invalidate_patient_exports
    from apps.notifications.services import revoke_push_subscriptions
    from apps.operations.models import TombstoneKind
    from apps.operations.tombstones import record_deletion_tombstone

    now = now or timezone.now()
    with transaction.atomic():
        access = authorize_patient(patient_id, actor, Capability.OWNER, lock=True)
        patient = access.patient
        invalidate_patient_exports(patient)
        for document in Document.objects.filter(patient=patient).order_by("pk"):
            if document.deleted_at is None or document.trashed_at is not None:
                request_document_deletion(patient, document.pk, actor=access.actor, dispatch=document_dispatch, now=now)
            else:
                job, _ = DocumentDeletionJob.objects.get_or_create(document=document, defaults={"object_key": document.original_object_key})
                transaction.on_commit(lambda identity=job.pk: document_dispatch(identity))
        patient.deleted_at = now
        patient.save(update_fields=["deleted_at", "updated_at"])
        PatientMembership.objects.filter(patient=patient, revoked_at__isnull=True).update(revoked_at=now)
        revoke_push_subscriptions(patient)
        record_deletion_tombstone(TombstoneKind.PATIENT, patient.pk, now=now)
        from apps.operations.audit import record_audit_event
        record_audit_event(access.actor.pk, "patient_deletion_requested", patient.pk, "scheduled")
        job, _ = PatientDeletionJob.objects.get_or_create(patient=patient, defaults={"requested_by": access.actor})
    return job


def purge_patient_deletions(*, patient_ids=None):
    from apps.documents.models import Document
    from apps.exports.models import ExportJob

    query = PatientDeletionJob.objects.order_by("patient_id")
    if patient_ids is not None:
        query = query.filter(patient_id__in=patient_ids)
    completed = 0
    for identity in list(query.values_list("patient_id", flat=True)):
        with transaction.atomic():
            patient = Patient.objects.select_for_update().filter(pk=identity, deleted_at__isnull=False).first()
            if patient is None or Document.objects.filter(patient=patient).exists():
                continue
            if ExportJob.objects.filter(patient=patient, cleanup_pending=True).exists():
                continue
            patient.delete()
            completed += 1
    return completed
