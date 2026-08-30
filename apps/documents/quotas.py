from dataclasses import dataclass

from django.db import connection, transaction
from django.db.models import Sum

from .models import Document, PatientUploadQuota


@dataclass(frozen=True)
class QuotaProposal:
    batch_files: int = 0
    batch_pages: int = 0
    documents: int = 0
    document_pages: int = 0
    storage_bytes: int = 0


class QuotaExceeded(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class QuotaLockRequired(RuntimeError):
    pass


class InvalidQuotaProposal(ValueError):
    pass


def lock_patient_quota(patient):
    """Lock the patient quota row; finalizers take this before any batch lock."""
    if not connection.in_atomic_block:
        raise QuotaLockRequired("lock_patient_quota requires transaction.atomic()")
    # The patient row serializes first-row creation across PostgreSQL finalizers.
    locked_patient = type(patient).objects.select_for_update().get(pk=patient.pk)
    if not type(patient).objects.filter(pk=locked_patient.pk, account__is_active=True).exists():
        raise QuotaExceeded("account_inactive")
    quota, _ = PatientUploadQuota.objects.get_or_create(patient=locked_patient)
    return PatientUploadQuota.objects.select_for_update().get(pk=quota.pk)


def _proposal(value):
    if isinstance(value, QuotaProposal):
        return value
    if isinstance(value, dict):
        return QuotaProposal(**value)
    raise InvalidQuotaProposal("proposed quota must be a QuotaProposal or mapping")


def _usage(patient):
    active = Document.objects.filter(patient=patient, deleted_at__isnull=True)
    active_totals = active.aggregate(pages=Sum("page_count"))
    retained_totals = Document.objects.filter(patient=patient, purged_at__isnull=True).aggregate(bytes=Sum("byte_size"))
    return active.count(), active_totals["pages"] or 0, retained_totals["bytes"] or 0


def check_upload_quota(patient, proposed, *, quota=None):
    """Recompute durable usage; Task 3 calls it while holding quota then batch locks."""
    proposal = _proposal(proposed)
    if min(proposal.batch_files, proposal.batch_pages, proposal.documents, proposal.document_pages, proposal.storage_bytes) < 0:
        raise InvalidQuotaProposal("quota proposal values must be non-negative")
    with transaction.atomic():
        if quota is not None and quota.patient_id != patient.pk:
            raise InvalidQuotaProposal("quota row does not belong to patient")
        locked = (
            PatientUploadQuota.objects.select_for_update().get(pk=quota.pk)
            if quota is not None
            else lock_patient_quota(patient)
        )
        documents, pages, storage_bytes = _usage(patient)
        if proposal.batch_files > locked.batch_file_limit:
            raise QuotaExceeded("batch_file_limit")
        if proposal.batch_pages > locked.batch_page_limit:
            raise QuotaExceeded("batch_page_limit")
        if documents + proposal.documents > locked.document_limit:
            raise QuotaExceeded("document_limit")
        if pages + proposal.document_pages > locked.page_limit:
            raise QuotaExceeded("page_limit")
        if storage_bytes + proposal.storage_bytes > locked.storage_byte_limit:
            raise QuotaExceeded("storage_limit")
        return locked
