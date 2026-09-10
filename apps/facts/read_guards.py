"""Bind complete source-dependent HTML to state captured before building it.

Use batched raw model rows, rather than reusing cached model instances or a
fresh token acquired after constructing old form choices. Snapshots are private
digests, not output projections, and introduce no transaction across rendering.
"""
from functools import wraps

from django.http import HttpResponse

from apps.accounts.models import Account
from apps.core.responses import protect_sensitive_html
from apps.documents.models import Document, DocumentPage
from apps.patients.access import Capability, authorize_patient
from apps.patients.models import Patient
from apps.processing.models import DocumentSummary, OcrBlock, ParsingVersion, SourceEvidence

from .models import (ClinicalExtraction, ClinicalReport, ClinicalReportRevision, ClinicalReportSpan,
                     Fact, FactExtraction, FactRevision, FactSourceFragment, LateralityScopeBinding,
                     LateralityScopeOperation, LateralityScopeOperationRevision, LateralityScopeRange)
from .readmodels import digest


def _rows(model, **filters):
    return list(model.objects.filter(**filters).order_by('pk').values())


def _document_scope(patient, route):
    if 'document_id' in route:
        return [route['document_id']]
    for key, model in [('fact_id', Fact), ('report_id', ClinicalReport), ('scope_operation_id', LateralityScopeOperation)]:
        if key in route:
            return list(model.objects.filter(pk=route[key], document__patient=patient).values_list('document_id', flat=True))
    return None  # List pages compare the complete set, including added reports.


def source_snapshot(patient, *, document_ids=None, lesions=False):
    documents = Document.objects.filter(patient_id=patient.pk)
    if document_ids is not None:
        documents = documents.filter(pk__in=document_ids)
    ids = list(documents.order_by('pk').values_list('pk', flat=True))
    tables = [
        (Patient, {'pk': patient.pk}), (Document, {'pk__in': ids}),
        (DocumentPage, {'document_id__in': ids}), (ParsingVersion, {'document_id__in': ids}),
        (DocumentSummary, {'parsing_version__document_id__in': ids}),
        # Original blocks also cover manual excerpt page context. Never use
        # only the selected child and omit its parent, report, or old version.
        (OcrBlock, {'parsing_version__document_id__in': ids}),
        (SourceEvidence, {'parsing_version__document_id__in': ids}),
        (Fact, {'document_id__in': ids}), (FactRevision, {'fact__document_id__in': ids}),
        (FactExtraction, {'parsing_version__document_id__in': ids}),
        (ClinicalReport, {'document_id__in': ids}),
        (ClinicalReportRevision, {'report__document_id__in': ids}),
        (ClinicalReportSpan, {'report__document_id__in': ids}),
        (ClinicalExtraction, {'parsing_version__document_id__in': ids}),
        (FactSourceFragment, {'fact__document_id__in': ids}),
        (LateralityScopeBinding, {'fact__document_id__in': ids}),
        (LateralityScopeRange, {'binding__fact__document_id__in': ids}),
        (LateralityScopeOperation, {'document_id__in': ids}),
        (LateralityScopeOperationRevision, {'operation__document_id__in': ids}),
    ]
    if lesions:
        from apps.lesions.models import (Lesion, LesionMatchProposal, LesionObservation, LesionObservationRevision,
                                        LesionOperation, LesionProposalRevision, LesionRevision)
        tables += [(model, {'patient_id': patient.pk})
                   for model in (Lesion, LesionObservation, LesionMatchProposal, LesionOperation)]
        tables += [(model, {'operation__patient_id': patient.pk})
                   for model in (LesionRevision, LesionObservationRevision, LesionProposalRevision)]
    material = {model._meta.label_lower: _rows(model, **filters) for model, filters in tables}
    authors = {value for rows in material.values() for row in rows for key, value in row.items()
               if (key == 'author_id' or key.endswith('_by_id')) and value is not None}
    # Only actor identity and liveness enter the guard, never account credentials.
    material['authors'] = list(Account.objects.filter(pk__in=authors).order_by('pk').values('pk', 'is_active'))
    return digest(material)


def _access_state(access):
    member = access.membership
    return (access.patient.pk, access.actor.pk, member.pk, member.role, member.revision)


def source_read(view=None, *, lesions=False):
    """Wrap every HTML entry inside patient_required, before any source reads."""
    if view is None:
        return lambda function: source_read(function, lesions=lesions)
    @wraps(view)
    def guarded(request, *args, **kwargs):
        document_ids = None if lesions else _document_scope(request.patient, kwargs)
        access = _access_state(request.patient_access)
        before = source_snapshot(request.patient, document_ids=document_ids, lesions=lesions)
        response = view(request, *args, **kwargs)
        if response.status_code in {301, 302, 303, 307, 308}:
            return response  # Successful mutations redirect; their services own write guards.
        after = source_snapshot(request.patient, document_ids=document_ids, lesions=lesions)
        # Permission is the last live read, including invalid write forms. Do
        # not release old form choices after revocation during source checking.
        capability = Capability.READ if request.method in {'GET', 'HEAD', 'OPTIONS'} else Capability.WRITE
        current = authorize_patient(request.patient.pk, request.user, capability)
        if _access_state(current) != access or after != before:
            # A rendered HttpResponse owns bytes only. Do not emit the old body
            # or close the active request's DB connection through request_finished.
            # Pathology may already have rejected stale material as Gone. Keep
            # that contract while still replacing the body and checking access.
            status = 410 if response.status_code == 410 else 409
            return protect_sensitive_html(HttpResponse('来源或修订记录已变化，请刷新页面后重新核对。', status=status))
        return response
    return guarded
