import hashlib
import io

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.documents.models import Document, DocumentPage, UploadBatch, UploadItem, ProcessingRun
from apps.processing.models import ParsingVersion
from apps.processing.runner import run_processing, ExecutionState
from apps.processing.value_objects import OcrPage
from tests.processing.test_material_classification import synthetic_scene
from tests.processing.test_pipeline import _document_and_run, _pipeline, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def photo_document(django_user_model):
    document, run = _document_and_run(django_user_model)
    output = io.BytesIO()
    synthetic_scene().save(output, format='PNG')
    store = _Store(output.getvalue())
    provider = OcrPage(1,640,480,(),'fixture','1')
    assert run_processing(run.pk,_pipeline(store,provider)).state == ExecutionState.NO_STRUCTURED_RESULT
    return document, ParsingVersion.objects.get(processing_run=run), store, provider


def test_pipeline_keeps_non_document_separate_from_ocr_failure_and_type(django_user_model):
    document, version, store, _ = photo_document(django_user_model)
    assessment = version.diagnostics.get('material', {})
    assert assessment.get('status') == 'NON_DOCUMENT'
    assert assessment['source_sha256'] == document.sha256
    assert version.document_summary.document_type == 'UNKNOWN'
    assert version.active and version.status == 'PUBLISHED'
    assert assessment['pages'][0]['precision'] == 'page'
    assert store.keys == [document.original_object_key]


def test_keep_reprocess_is_idempotent_and_preserves_original_and_automatic_history(django_user_model):
    from apps.processing.material_review import review_material
    document, version, store, provider = photo_document(django_user_model)
    original_hash = hashlib.sha256(store.payload).hexdigest()
    identity = (document.sha256,document.original_object_key,document.byte_size,document.page_count)
    counts = tuple(model.objects.count() for model in (Document,DocumentPage,UploadBatch,UploadItem))
    queued=[]
    kwargs = dict(actor=document.patient.account, action='KEEP_DOCUMENT', expected_version=str(version.pk), expected_revision=0, dispatch=queued.append)
    decision=review_material(document.patient,document.pk,**kwargs)
    repeat=review_material(document.patient,document.pk,**kwargs)
    assert repeat.pk == decision.pk and queued == [decision.processing_run_id]
    assert decision.author_id == document.patient.account_id
    assert decision.parsing_version_id == version.pk
    document.refresh_from_db()
    assert document.material_override == 'KEEP_DOCUMENT' and document.material_revision == 1
    assert tuple(model.objects.count() for model in (Document,DocumentPage,UploadBatch,UploadItem)) == counts
    assert (document.sha256,document.original_object_key,document.byte_size,document.page_count) == identity
    assert hashlib.sha256(store.payload).hexdigest() == original_hash
    assert run_processing(decision.processing_run_id,_pipeline(store,provider)).state == ExecutionState.NO_STRUCTURED_RESULT
    version.refresh_from_db()
    assert version.diagnostics['material']['status'] == 'NON_DOCUMENT' and not version.active
    from apps.processing.material_review import material_state
    state=material_state(Document.objects.get(pk=document.pk))
    assert state['status'] == 'DOCUMENT' and state['automatic_status'] == 'NON_DOCUMENT'
    assert state['override'] == 'KEEP_DOCUMENT'
    assert ProcessingRun.objects.filter(document=document).count() == 2


def test_stale_version_conflict_and_reset_keep_distinct_audit_records(django_user_model):
    from apps.processing.material_review import review_material, MaterialReviewConflict
    document, version, store, provider=photo_document(django_user_model)
    kwargs=dict(actor=document.patient.account,expected_version=str(version.pk),dispatch=lambda _:None)
    decision=review_material(document.patient,document.pk,action='KEEP_DOCUMENT',expected_revision=0,**kwargs)
    run_processing(decision.processing_run_id,_pipeline(store,provider))
    with pytest.raises(MaterialReviewConflict):
        review_material(document.patient,document.pk,action='AUTO',expected_revision=1,**kwargs)
    active=ParsingVersion.objects.get(document=document,active=True)
    kwargs['expected_version']=str(active.pk)
    reset=review_material(document.patient,document.pk,action='AUTO',expected_revision=1,**kwargs)
    assert reset.sequence == 2 and reset.processing_run_id is None
    document.refresh_from_db()
    assert document.material_override == 'AUTO' and document.material_revision == 2
    assert document.material_decisions.count() == 2


def test_material_review_rejects_cross_patient_inactive_actor_and_deleted_document(django_user_model):
    from apps.processing.material_review import review_material
    document, version, _, _=photo_document(django_user_model)
    other,_=_document_and_run(django_user_model)
    kwargs=dict(action='KEEP_DOCUMENT',expected_version=str(version.pk),expected_revision=0,dispatch=lambda _:None)
    with pytest.raises(PermissionDenied):
        review_material(other.patient,document.pk,actor=other.patient.account,**kwargs)
    stale=document.patient.account
    type(stale).objects.filter(pk=stale.pk).update(is_active=False)
    with pytest.raises(PermissionDenied):
        review_material(document.patient,document.pk,actor=stale,**kwargs)
    type(stale).objects.filter(pk=stale.pk).update(is_active=True)
    Document.objects.filter(pk=document.pk).update(deleted_at=timezone.now())
    with pytest.raises(PermissionDenied):
        review_material(document.patient,document.pk,actor=stale,**kwargs)
