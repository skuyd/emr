"""Live parent revisions and author history constrain the actual Fact services."""
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.models import Fact, LateralityScopeBinding, LateralityScopeRange
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from tests.facts.test_imaging_quantitative import imaging
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def fixture(django_user_model, name='side-review'):
    _, patient, document, _, _ = imaging(django_user_model, '纵隔及双肺门见淋巴结，短径12mm。', name=name)
    return patient, document.clinical_reports.get(), document.facts.get(field_key='lesion.site'), document.facts.get(field_key='lesion.scoped_laterality')


def test_child_confirmation_requires_a_current_confirmed_parent(django_user_model):
    patient, _, parent, child = fixture(django_user_model)
    with pytest.raises(FactConflict):
        confirm(patient, child)
    assert child.revisions.count() == 0
    confirm(patient, parent)
    confirm(patient, child)
    assert effective_fact(child)['usable']


@pytest.mark.parametrize('action', ['CONFIRM', 'CORRECT', 'DEFER', 'EXCLUDE', 'REVOKE'])
def test_old_child_form_rejects_parent_revision_even_when_original_text_is_equal(django_user_model, action):
    patient, _, parent, child = fixture(django_user_model, 'side-stale-' + action)
    confirm(patient, parent)
    confirm(patient, child)
    before = effective_fact(child)
    confirm(patient, parent)  # Same value, a distinct real review head.
    with pytest.raises(FactConflict):
        revise_fact(patient, child.pk, actor=patient.account, action=action,
                    expected_revision=1, expected_source=before['current_source_token'], checked_original=True)
    child.refresh_from_db()
    assert child.revision_number == 1 and not effective_fact(child)['usable']


def test_correction_cannot_rename_or_reorder_an_immutable_named_member(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'side-member-identity')
    confirm(patient, parent)
    confirm(patient, child)
    original = deepcopy(child.automatic_content)
    value = deepcopy(original['value'])
    value['members'][0]['site_text'] = '合成另一个部位'
    with pytest.raises(ValidationError):
        confirm(patient, child, 'CORRECT', changes={'value': value, 'raw_value': original['raw_value']})
    child.refresh_from_db()
    assert child.revision_number == 1 and child.automatic_content == original and effective_fact(child)['usable']


@pytest.mark.parametrize('previously_excluded', [False, True])
def test_parent_report_exclude_undo_never_revives_an_old_scope_confirmation(django_user_model, previously_excluded):
    patient, report, parent, child = fixture(django_user_model, 'side-parent-undo-' + str(previously_excluded))
    confirm(patient, parent)
    confirm(patient, child)
    if previously_excluded:
        confirm(patient, child, 'EXCLUDE')
    original = deepcopy(child.automatic_content)
    for action in ('EXCLUDE', 'UNDO'):
        report.refresh_from_db()
        revise_report(patient, actor=patient.account, report_id=report.pk, action=action,
                      expected_revision=report.revision_number, expected_source=report_source_token(report))
    child.refresh_from_db()
    assert effective_fact(child)['status'] == ('EXCLUDED' if previously_excluded else 'PENDING')
    assert not effective_fact(child)['usable'] and child.automatic_content == original


def test_parent_source_change_and_member_loss_invalidate_the_child(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'side-parent-moved')
    confirm(patient, parent)
    confirm(patient, child)
    original = deepcopy(child.automatic_content)
    confirm(patient, parent, 'CORRECT', changes={'value': {'text': '纵隔'}, 'raw_value': '纵隔见淋巴结。'})
    assert effective_fact(child)['laterality_scope']['scope_state'] == 'INVALID'
    with pytest.raises(FactConflict):
        confirm(patient, child)
    child.refresh_from_db()
    assert child.automatic_content == original


def test_original_range_cannot_switch_to_another_block_or_parent(django_user_model):
    patient, report, parent, child = fixture(django_user_model, 'side-range-forgery')
    binding = child.laterality_scope_binding
    source = binding.ranges.get()
    source.reading_order += 1
    with pytest.raises(ValidationError):
        source.clean()
    with pytest.raises(ValidationError):
        source.save()
    _, _, foreign, _ = fixture(django_user_model, 'side-foreign-parent')
    binding.parent_site = foreign
    binding.original_parent_id = foreign.pk
    with pytest.raises(ValidationError):
        binding.clean()
    assert LateralityScopeRange.objects.get(pk=source.pk).reading_order != source.reading_order
    assert LateralityScopeBinding.objects.get(pk=binding.pk).parent_site_id == parent.pk


@pytest.mark.parametrize('target', ['parent', 'child'])
def test_intermediate_review_author_purge_removes_scope_usability(django_user_model, target):
    patient, _, parent, child = fixture(django_user_model, 'side-author-' + target)
    _, other = _patient(django_user_model, 'side-author-collaborator-' + target)
    actor = other.account
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    confirm(patient, parent)
    record = parent if target == 'parent' else child
    revise_fact(patient, record.pk, actor=actor, action='CONFIRM', expected_revision=record.revisions.count(),
                expected_source=effective_fact(record)['current_source_token'], checked_original=True)
    confirm(patient, record)
    confirm(patient, child)
    assert effective_fact(child)['usable']
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    child.refresh_from_db()
    assert not effective_fact(child)['usable'] and not effective_fact(child)['source_valid']


def test_parent_deletion_keeps_original_parent_identity_but_invalidates_scope(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'side-deleted-parent')
    confirm(patient, parent)
    confirm(patient, child)
    parent_id = parent.pk
    Fact.objects.filter(pk=parent_id).delete()
    child.refresh_from_db()
    binding = LateralityScopeBinding.objects.get(fact=child)
    assert binding.parent_site_id is None and binding.original_parent_id == parent_id
    assert not effective_fact(child)['usable']


@pytest.mark.parametrize('change', ['report_author_purge', 'source_block_order'])
def test_report_restore_rejects_changed_original_scope_or_exclusion_author(django_user_model, change):
    patient, report, parent, child = fixture(django_user_model, 'side-restore-proof-' + change)
    confirm(patient, parent)
    confirm(patient, child)
    _, other = _patient(django_user_model, 'side-restore-actor-' + change)
    actor = other.account
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    revise_report(patient, actor=actor, report_id=report.pk, action='EXCLUDE', expected_revision=0,
                  expected_source=report_source_token(report))
    if change == 'report_author_purge':
        job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    else:
        source = child.laterality_scope_binding.ranges.get()
        block = source.parent_fragment.ocr_block
        type(block).objects.filter(pk=block.pk).update(reading_order=block.reading_order + 50)
    before = list(report.fields.order_by('pk').values_list('pk', 'revision_number'))
    report.refresh_from_db()
    with pytest.raises(FactConflict):
        revise_report(patient, actor=patient.account, report_id=report.pk, action='UNDO',
                      expected_revision=report.revision_number, expected_source=report_source_token(report))
    assert list(report.fields.order_by('pk').values_list('pk', 'revision_number')) == before


def test_revoke_and_undo_keep_existing_meanings_but_not_after_parent_change(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'side-revoke-undo')
    confirm(patient, parent)
    confirm(patient, child)
    confirm(patient, child, 'REVOKE')
    assert effective_fact(child)['status'] == 'PENDING'
    confirm(patient, child, 'UNDO')
    assert effective_fact(child)['usable']
    confirm(patient, child, 'REVOKE')
    confirm(patient, parent)
    with pytest.raises(FactConflict):
        confirm(patient, child, 'UNDO')
    assert not effective_fact(child)['usable']
