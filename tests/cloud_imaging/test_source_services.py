from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db
FIRST_URL = 'https://images.example.invalid/view?key=SYNTHETIC_FIRST'
SECOND_URL = 'https://images.example.invalid/view?key=SYNTHETIC_CORRECTION'


def _manual(django_user_model, *, actor=None):
    from apps.cloud_imaging.readmodels import document_snapshot
    from apps.cloud_imaging.services import add_manual_source

    client, patient = _patient(django_user_model, 'cloud-manual-owner')
    document, pages = _document(patient, page_count=1)
    actor = actor or patient.account
    original = document_snapshot(patient, actor=patient.account, document_id=document.pk)
    source = add_manual_source(patient, actor=actor, document_id=document.pk, page_id=pages[0].pk,
                               url=FIRST_URL, title='原页访问入口', expected_source=original['input_token'], operation_id=uuid4())
    return client, patient, document, source


def _decide(patient, source, action, **extra):
    from apps.cloud_imaging.readmodels import source_details
    from apps.cloud_imaging.services import revise_source

    current = source_details(patient, actor=patient.account, source_id=source.pk)
    return revise_source(patient, actor=patient.account, source_id=source.pk, action=action,
                         expected_revision=current['revision_number'], expected_source=current['source_token'],
                         operation_id=uuid4(), checked_original=True, **extra)


def test_manual_add_confirm_correct_exclude_and_undo_preserve_each_original(django_user_model):
    from apps.cloud_imaging.readmodels import source_details

    _, patient, document, source = _manual(django_user_model)
    assert source.status == 'PENDING' and source.revision_number == 1
    assert source.evidence.kind == 'MANUAL' and source.evidence.ocr_block_id is None
    assert source.evidence.polygon is None and source.evidence.payload == FIRST_URL
    initial_evidence = source.evidence
    source = _decide(patient, source, 'CONFIRM')
    assert source_details(patient, actor=patient.account, source_id=source.pk)['usable']
    source = _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL, 'title': '更正入口'})
    assert source.current_url == SECOND_URL and source.evidence_id != initial_evidence.pk
    initial_evidence.refresh_from_db()
    assert initial_evidence.payload == FIRST_URL
    assert not document.facts.exists()
    source = _decide(patient, source, 'EXCLUDE')
    assert source.status == 'EXCLUDED'
    source = _decide(patient, source, 'UNDO')
    assert source.status == 'PENDING'
    assert source.current_url == SECOND_URL and source.revision_number == 5
    assert list(source.revisions.values_list('sequence', flat=True)) == [1, 2, 3, 4, 5]
    assert set(source.revisions.values_list('author_id', flat=True)) == {patient.account_id}


def test_same_decision_is_idempotent_but_changed_body_or_old_revision_conflicts(django_user_model):
    from apps.cloud_imaging.readmodels import source_details
    from apps.cloud_imaging.services import CloudConflict, revise_source

    _, patient, _, source = _manual(django_user_model)
    current = source_details(patient, actor=patient.account, source_id=source.pk)
    params = dict(actor=patient.account, source_id=source.pk, action='CONFIRM', expected_revision=1,
                  expected_source=current['source_token'], operation_id=uuid4(), checked_original=True)
    first = revise_source(patient, **params)
    repeated = revise_source(patient, **params)
    assert first.pk == repeated.pk and repeated.revision_number == 2
    assert source.revisions.count() == 2
    with pytest.raises(CloudConflict):
        revise_source(patient, **{**params, 'action': 'EXCLUDE'})
    with pytest.raises(CloudConflict):
        revise_source(patient, **{**params, 'operation_id': uuid4()})


def test_viewer_and_foreign_user_cannot_decide_but_editor_is_the_actual_author(django_user_model):
    from apps.cloud_imaging.readmodels import source_details
    from apps.cloud_imaging.services import revise_source

    _, patient, _, source = _manual(django_user_model)
    _, other = _patient(django_user_model, 'cloud-other-account')
    member = PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    current = source_details(patient, actor=other.account, source_id=source.pk)
    params = dict(actor=other.account, source_id=source.pk, action='CONFIRM', expected_revision=1,
                  expected_source=current['source_token'], operation_id=uuid4(), checked_original=True)
    with pytest.raises(PermissionDenied):
        revise_source(patient, **params)
    member.role = 'EDITOR'
    member.save(update_fields=['role'])
    source = revise_source(patient, **params)
    assert source.updated_by_id == other.account_id and source.revisions.last().author_id == other.account_id
    with pytest.raises(PermissionDenied):
        source_details(other, actor=other.account, source_id=source.pk)


def test_manual_add_rejects_changed_original_and_foreign_page(django_user_model):
    from apps.cloud_imaging.readmodels import document_snapshot
    from apps.cloud_imaging.services import CloudConflict, add_manual_source

    _, patient = _patient(django_user_model, 'cloud-manual-input')
    document, pages = _document(patient, page_count=1)
    original = document_snapshot(patient, actor=patient.account, document_id=document.pk)
    other, other_pages = _document(patient, page_count=1)
    params = dict(actor=patient.account, document_id=document.pk, url=FIRST_URL, operation_id=uuid4(),
                  expected_source=original['input_token'])
    with pytest.raises(ValidationError):
        add_manual_source(patient, page_id=other_pages[0].pk, **params)
    type(document).objects.filter(pk=document.pk).update(material_revision=1)
    with pytest.raises(CloudConflict):
        add_manual_source(patient, page_id=pages[0].pk, **params)


def test_deleted_contributor_is_anonymized_without_deleting_other_family_proofs(django_user_model):
    from apps.cloud_imaging.models import CloudImagingEvidence, CloudImagingSource
    from apps.cloud_imaging.readmodels import source_details

    _, patient, _, source = _manual(django_user_model)
    _, contributor = _patient(django_user_model, 'cloud-contributor')
    member = PatientMembership.objects.create(patient=patient, account=contributor.account, role='EDITOR')
    from apps.cloud_imaging.services import revise_source

    current = source_details(patient, actor=patient.account, source_id=source.pk)
    revise_source(patient, actor=contributor.account, source_id=source.pk, action='CONFIRM', expected_revision=1,
                  expected_source=current['source_token'], operation_id=uuid4(), checked_original=True)
    deleted_identity = contributor.account_id
    from apps.accounts.deletion import AccountDeletionOutcome, request_account_deletion, purge_account_deletion

    deletion = request_account_deletion(deleted_identity, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
    source.refresh_from_db()
    assert source.updated_by_id is None and source.revisions.last().author_id is None
    assert CloudImagingSource.objects.filter(pk=source.pk).exists()
    assert CloudImagingEvidence.objects.filter(pk=source.evidence_id).exists()
    current = source_details(patient, actor=patient.account, source_id=source.pk)
    assert not current['usable']
    assert str(deleted_identity) not in str(current)


def test_document_aggregate_hard_delete_cleans_source_proofs_without_restrict_dead_end(django_user_model):
    from apps.cloud_imaging.models import CloudImagingEvidence, CloudImagingRevision, CloudImagingSource

    _, _, document, source = _manual(django_user_model)
    identity, evidence_id = source.pk, source.evidence_id
    document.delete()
    assert not CloudImagingSource.objects.filter(pk=identity).exists()
    assert not CloudImagingRevision.objects.filter(source_id=identity).exists()
    assert not CloudImagingEvidence.objects.filter(pk=evidence_id).exists()
