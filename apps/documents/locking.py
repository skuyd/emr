"""Shared lock order for document aggregates: batches, document, then runs/jobs."""

from django.db import connection

from apps.patients.models import Patient

from .models import Document, UploadBatch, UploadItem


def lock_document_aggregate(document_id, *, patient_id=None, include_references=False):
    if not connection.in_atomic_block:
        raise RuntimeError("Document aggregate locks require transaction.atomic()")
    query = Document.objects.filter(pk=document_id)
    if patient_id is not None:
        query = query.filter(patient_id=patient_id)
    identity = query.values("batch_id", "patient_id").first()
    if identity is None:
        return None, ()
    if include_references:
        # Upload finalization locks the patient before attaching duplicate
        # UploadItems. Hold the same guard while collecting and removing all
        # references, otherwise a new batch could appear after our snapshot.
        if Patient.objects.select_for_update().filter(pk=identity["patient_id"]).first() is None:
            return None, ()
    batch_ids = {identity["batch_id"]}
    if include_references:
        batch_ids.update(UploadItem.objects.filter(document_id=document_id).values_list("batch_id", flat=True))
    batches = tuple(UploadBatch.objects.select_for_update().filter(pk__in=batch_ids).order_by("pk"))
    document = query.select_for_update().first()
    return document, batches
