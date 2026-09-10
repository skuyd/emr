"""Bind rendered output choices to all domains, without locking across rendering."""
from functools import wraps

from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import Account
from apps.core.responses import protect_sensitive_html
from apps.facts.read_guards import source_snapshot
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient
from apps.patients.models import PatientMembership


@sensitive_variables()
def source_stamp(patient):
    from apps.cancer_ordering import models as cancer
    from apps.cloud_imaging import models as cloud

    # Raw generations include changes that another domain's derived resolver
    # legitimately ignores. Output jobs/shares are not source material.
    tables = [
        (cancer.NarrativeSource, {"document__patient": patient}),
        (cancer.CancerCandidate, {"patient": patient}),
        (cancer.CandidateRevision, {"candidate__patient": patient}),
        (cancer.OccurrenceReview, {"document__patient": patient}),
        (cancer.CollectionRun, {"patient": patient}),
        (cancer.CollectionCandidate, {"collection__patient": patient}),
        (cancer.NarrativeDependency, {"collection__patient": patient}),
        (cancer.DisplaySelection, {"patient": patient}),
        (cancer.SelectionRevision, {"selection__patient": patient}),
        (cloud.CloudImagingScan, {"patient": patient}),
        (cloud.CloudImagingSource, {"patient": patient}),
        (cloud.CloudImagingEvidence, {"document__patient": patient}),
        (cloud.CloudImagingRevision, {"source__patient": patient}),
        (PatientMembership, {"patient": patient}),
    ]
    material = {model._meta.label_lower: list(model.objects.filter(**filters).order_by("pk").values())
                for model, filters in tables}
    authors = {value for rows in material.values() for row in rows for key, value in row.items()
               if (key == "author_id" or key.endswith("_by_id")) and value is not None}
    material["authors"] = list(Account.objects.filter(pk__in=authors).order_by("pk").values("pk", "is_active"))
    material["clinical_and_lesion_sources"] = source_snapshot(patient, lesions=True)
    return digest(material)


def _access_state(access):
    member = access.membership
    return (access.patient.pk, access.actor.pk, member.pk, member.role, member.revision)


def guard_choices(capability):
    def decorate(view):
        @wraps(view)
        @sensitive_variables()
        def guarded(request, *args, **kwargs):
            patient = kwargs.get("patient_id", getattr(request, "patient", None))
            with transaction.atomic():
                access = authorize_patient(patient, request.user, capability, lock=True)
                before_access = _access_state(access)
                before = source_stamp(access.patient)
            # Existing domain selection checks still run. The final raw stamp
            # follows all of them, so a later resolver cannot invalidate an
            # earlier domain's already-rendered choices unnoticed.
            response = view(request, *args, **kwargs)
            with transaction.atomic():
                current = authorize_patient(access.patient.pk, request.user, capability, lock=True)
                after = source_stamp(current.patient)
                current = authorize_patient(current.patient.pk, request.user, capability)
                if before != after or before_access != _access_state(current):
                    return protect_sensitive_html(HttpResponse(
                        "来源、修订或访问资格已变化，请刷新页面后重新选择。",
                        status=410 if response.status_code == 410 else 409,
                    ))
            return response
        return guarded
    return decorate
