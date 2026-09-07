from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from apps.documents.models import Document, PatientUploadQuota

from .models import Patient, PatientPreference, ProductFeedback


@dataclass(frozen=True)
class QuotaSummary:
    documents: int
    document_limit: int
    pages: int
    page_limit: int
    storage_bytes: int
    storage_byte_limit: int
    batch_file_limit: int
    batch_page_limit: int


def patient_preferences(patient, actor=None):
    account_id = getattr(actor, "pk", actor) if actor is not None else patient.account_id
    preference, _created = PatientPreference.objects.get_or_create(patient=patient, account_id=account_id)
    return preference


def quota_summary(patient):
    quota = PatientUploadQuota.objects.filter(patient=patient).first() or PatientUploadQuota(patient=patient)
    active = Document.objects.filter(patient=patient, deleted_at__isnull=True)
    retained = Document.objects.filter(patient=patient, purged_at__isnull=True)
    return QuotaSummary(
        documents=active.count(),
        document_limit=quota.document_limit,
        pages=active.aggregate(total=Sum("page_count"))["total"] or 0,
        page_limit=quota.page_limit,
        storage_bytes=retained.aggregate(total=Sum("byte_size"))["total"] or 0,
        storage_byte_limit=quota.storage_byte_limit,
        batch_file_limit=quota.batch_file_limit,
        batch_page_limit=quota.batch_page_limit,
    )


def update_display_name(patient_id, display_name, *, actor=None):
    with transaction.atomic():
        patient = Patient.objects.select_for_update().get(pk=patient_id)
        from .access import authorize_patient, owner_actor
        authorize_patient(patient, owner_actor(patient, actor), "write")
        patient.display_name = display_name
        patient.save(update_fields=["display_name", "updated_at"])
    return patient


def save_product_feedback(patient, message, *, actor=None):
    from .access import authorize_patient, owner_actor
    with transaction.atomic():
        access = authorize_patient(patient, owner_actor(patient, actor), lock=True)
        return ProductFeedback.objects.create(patient=patient, created_by=access.actor, category="GENERAL", message=message)
