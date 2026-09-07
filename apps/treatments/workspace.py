"""One authorized current material for calendar, cycles and export adapters."""
from django.db import transaction

from apps.documents.models import Document
from apps.facts.clinical_readmodels import report_material
from apps.patients.access import authorize_patient

from .laboratory import trusted_laboratory
from .readmodels import _trusted_material
from .records import _record_state
from .signals import digest


def trusted_workspace_material(patient, *, include_history=False):
    material = _trusted_material(patient, include_history=include_history)
    laboratory = trusted_laboratory(patient)
    documents = list(Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by("pk"))
    records = [_record_state(patient, "document", row.pk) for row in documents]
    reports = report_material(patient)
    records.extend(_record_state(patient, "report", row["id"]) for row in reports)
    records.extend(laboratory["records"])
    material.update({"records": records, "personal_changes": laboratory["personal_changes"]})
    material["fingerprint"] = digest({key: value for key, value in material.items() if key != "fingerprint"})
    return material


def workspace_material(patient, *, actor, include_history=False):
    with transaction.atomic():
        access = authorize_patient(patient, actor, "read", lock=True)
        material = trusted_workspace_material(access.patient, include_history=include_history)
    authorize_patient(access.patient, actor, "read")
    return material
