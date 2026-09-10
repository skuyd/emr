"""Explicit scope operations retain old facts and guard both review heads."""
from copy import deepcopy

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def api():
    from apps.facts import laterality_services
    return laterality_services


def expected(fact):
    fact.refresh_from_db()
    row = effective_fact(fact)
    return row['revision_number'], row['current_source_token']


def new_member(parent, child):
    value = deepcopy(child.automatic_content['value'])
    source = child.laterality_scope_binding.ranges.get()
    return value, [{'member_key': value['members'][0]['member_key'], 'parent_fragment_id': source.parent_fragment_id,
                    'start_offset': source.start_offset, 'end_offset': source.end_offset}]


def replace(patient, parent, child, *, confirm_new=False, actor=None, value=None, ranges=None, **overrides):
    default_value, default_ranges = new_member(parent, child)
    revision, source = expected(child)
    parent_revision, parent_source = expected(parent)
    args = dict(actor=actor or patient.account, fact_id=child.pk, parent_id=parent.pk,
                expected_revision=revision, expected_source=source,
                expected_parent_revision=parent_revision, expected_parent_source=parent_source,
                scope_kind='NAMED_MEMBERS_ONLY', value=value or default_value, ranges=ranges or default_ranges,
                checked_original=True, confirm=confirm_new)
    args.update(overrides)
    return api().replace_laterality_scope(patient, **args)


def undo(patient, event, *, actor=None):
    return api().undo_laterality_scope(patient, actor=actor or patient.account, operation_id=event.pk,
                                       expected_operation=api().operation_material(patient, actor=actor or patient.account,
                                                                                 operation_id=event.pk)['current_token'])


@pytest.mark.parametrize('confirm_new', [False, True])
def test_replacement_keeps_original_ranges_and_exact_affected_revisions(django_user_model, confirm_new):
    patient, _, parent, old = fixture(django_user_model, 'scope-replacement-' + str(confirm_new))
    confirm(patient, parent)
    confirm(patient, old)
    original = deepcopy(old.automatic_content)
    source_rows = list(old.source_fragments.values())
    binding_id = old.laterality_scope_binding.pk
    event = replace(patient, parent, old, confirm_new=confirm_new)
    old.refresh_from_db()
    new = event.new_fact
    assert new.pk != old.pk and new.origin == 'MANUAL' and new.created_by == patient.account
    assert new.laterality_scope_binding.origin == 'MANUAL'
    assert new.laterality_scope_binding.original_created_by_id == patient.account_id
    assert effective_fact(new)['status'] == ('CONFIRMED' if confirm_new else 'PENDING')
    assert effective_fact(new)['usable'] is confirm_new
    assert effective_fact(old)['status'] == 'EXCLUDED'
    assert old.automatic_content == original and old.laterality_scope_binding.pk == binding_id
    assert list(old.source_fragments.values()) == source_rows
    linked = list(event.revision_links.order_by('ordinal'))
    assert [row.revision.fact_id for row in linked] == ([old.pk, new.pk] if confirm_new else [old.pk])
    assert all(row.revision_id == row.original_revision_id for row in linked)
    assert event.after_guard['valid']


@pytest.mark.parametrize('change', ['child_head', 'parent_head', 'parent_token', 'unchecked', 'foreign_parent', 'outside_range'])
def test_replacement_refuses_stale_or_unchecked_source_without_partial_rows(django_user_model, change):
    patient, _, parent, old = fixture(django_user_model, 'scope-invalid-' + change)
    confirm(patient, parent)
    args = {}
    if change == 'child_head':
        args['expected_revision'] = 50
    elif change == 'parent_head':
        args['expected_parent_revision'] = 50
    elif change == 'parent_token':
        args['expected_parent_source'] = 'not-current'
    elif change == 'unchecked':
        args['checked_original'] = False
    elif change == 'foreign_parent':
        _, _, foreign, _ = fixture(django_user_model, 'scope-another-patient')
        args['parent_id'] = foreign.pk
    else:
        value, ranges = new_member(parent, old)
        ranges[0]['end_offset'] = 50000
        args.update(value=value, ranges=ranges)
    before = list(Fact.objects.order_by('pk').values_list('pk', 'revision_number'))
    with pytest.raises((FactConflict, ValidationError, PermissionDenied)):
        replace(patient, parent, old, **args)
    assert list(Fact.objects.order_by('pk').values_list('pk', 'revision_number')) == before


@pytest.mark.parametrize('already_excluded', [False, True])
def test_undo_replacement_returns_old_only_pending_and_never_revives_confirmation(django_user_model, already_excluded):
    patient, _, parent, old = fixture(django_user_model, 'scope-replacement-undo-' + str(already_excluded))
    confirm(patient, parent)
    confirm(patient, old)
    if already_excluded:
        confirm(patient, old, 'EXCLUDE')
    event = replace(patient, parent, old, confirm_new=True)
    reversed_event = undo(patient, event)
    old.refresh_from_db()
    event.new_fact.refresh_from_db()
    assert reversed_event.reverses_id == event.pk
    assert effective_fact(old)['status'] == ('EXCLUDED' if already_excluded else 'PENDING')
    assert not effective_fact(old)['usable']
    assert effective_fact(event.new_fact)['status'] == 'EXCLUDED'
    with pytest.raises(FactConflict):
        undo(patient, event)


@pytest.mark.parametrize('change', ['parent_review', 'new_child_review', 'block_order', 'operation_author_purge'])
def test_undo_replacement_refuses_later_parent_child_source_or_author_change(django_user_model, change):
    patient, _, parent, old = fixture(django_user_model, 'scope-replacement-guard-' + change)
    confirm(patient, parent)
    _, actor_patient = _patient(django_user_model, 'scope-operation-actor-' + change)
    actor = actor_patient.account
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    event = replace(patient, parent, old, actor=actor, confirm_new=True)
    if change == 'parent_review':
        confirm(patient, parent)
    elif change == 'new_child_review':
        confirm(patient, event.new_fact, 'DEFER')
    elif change == 'block_order':
        fragment = parent.source_fragments.first()
        block = fragment.ocr_block
        type(block).objects.filter(pk=block.pk).update(reading_order=block.reading_order + 99)
    else:
        job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    before = list(Fact.objects.order_by('pk').values_list('pk', 'revision_number'))
    with pytest.raises(FactConflict):
        undo(patient, event)
    assert list(Fact.objects.order_by('pk').values_list('pk', 'revision_number')) == before


def test_whole_attestation_is_explicit_replacement_and_keeps_legacy_scope_unknown(django_user_model):
    from tests.facts.test_imaging_quantitative import imaging
    from apps.facts.models import LateralityScopeBinding
    _, patient, document, _, _ = imaging(django_user_model, '左肺见结节，长径12mm。', name='scope-whole-attestation')
    parent = document.facts.get(field_key='lesion.site')
    old = document.facts.get(field_key='lesion.laterality')
    LateralityScopeBinding.objects.filter(fact=old).delete()  # Synthetic pre-scope legacy record.
    confirm(patient, parent)
    confirm(patient, old)
    assert effective_fact(old)['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'
    original = deepcopy(old.automatic_content)
    fragment = parent.source_fragments.get()
    start = fragment.ocr_block.text.index('左肺')
    pr, ps = expected(parent)
    cr, cs = expected(old)
    event = api().attest_whole_laterality(patient, actor=patient.account, fact_id=old.pk, parent_id=parent.pk,
        expected_revision=cr, expected_source=cs, expected_parent_revision=pr, expected_parent_source=ps,
        ranges=[{'member_key': 'whole', 'parent_fragment_id': fragment.pk, 'start_offset': start, 'end_offset': start + 2}],
        checked_original=True, confirm=True)
    assert event.action == 'ATTEST'
    assert effective_fact(event.new_fact)['laterality_scope']['scope_state'] == 'WHOLE_ENTITY'
    assert effective_fact(event.new_fact)['usable']
    old.refresh_from_db()
    assert old.automatic_content == original and not hasattr(old, 'laterality_scope_binding')
    undo(patient, event)
    old.refresh_from_db()
    assert effective_fact(old)['status'] == 'PENDING'
    assert effective_fact(old)['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'


def test_read_only_family_member_cannot_replace_or_read_other_patient_operation(django_user_model):
    patient, _, parent, old = fixture(django_user_model, 'scope-read-permission')
    confirm(patient, parent)
    _, other = _patient(django_user_model, 'scope-reader')
    PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    with pytest.raises(PermissionDenied):
        replace(patient, parent, old, actor=other.account)
    event = replace(patient, parent, old)
    with pytest.raises(PermissionDenied):
        api().operation_material(other, actor=other.account, operation_id=event.pk)


def test_no_ocr_scope_uses_actual_page_and_author_and_first_parse_invalidates_it(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import create_manual_report, add_manual_clinical_field
    from tests.documents.test_detail_viewer import _document
    from tests.facts.test_clinical_foundation import clinical_fixture
    from tests.facts.test_scoped_laterality import named_value
    _, patient = _patient(django_user_model, 'scope-manual-page')
    document, _ = _document(patient, status='PROCESSING_FAILED')
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk,
        spans=[{'page_number': 1}], title='合成影像报告', expected_lifecycle_revision=document.lifecycle_revision,
        expected_version_id=None)
    parent = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key='lesion:manual',
        field_key='lesion.site', value={'text': '纵隔及双肺门'},
        fragments=[{'page_number': 1, 'raw_text': '纵隔及双肺门见淋巴结。'}], expected_report_source=report_source_token(report))
    confirm(patient, parent)
    pr, ps = expected(parent)
    event = api().add_laterality_scope(patient, actor=patient.account, parent_id=parent.pk,
        expected_parent_revision=pr, expected_parent_source=ps, scope_kind='NAMED_MEMBERS_ONLY',
        value=named_value(('双肺门', 'BILATERAL')),
        ranges=[{'member_key': 'member:001', 'parent_fragment_id': parent.source_fragments.get().pk,
                 'page_number': 1, 'raw_text': '双肺门'}], checked_original=True, confirm=True)
    source = event.new_fact.laterality_scope_binding.ranges.get()
    assert source.source_kind == 'MANUAL_PAGE' and source.page_id_at_creation == parent.document_page_id
    assert source.block_id_at_creation is source.start_offset is source.end_offset is source.polygon is None
    assert source.child_fragment.ocr_block_id is None and event.new_fact.created_by_id == patient.account_id
    assert effective_fact(event.new_fact)['usable']
    clinical_fixture(django_user_model, document=document)
    event.new_fact.refresh_from_db()
    assert not effective_fact(event.new_fact)['usable']
    with pytest.raises(FactConflict):
        undo(patient, event)


@pytest.mark.parametrize('text,code', [('纵隔及双肺门', 'BILATERAL'), ('左肺及右肾', 'BILATERAL')])
def test_explicit_whole_attestation_cannot_promote_a_mixed_group(django_user_model, text, code):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import add_manual_clinical_field
    from tests.facts.test_imaging_quantitative import imaging
    _, patient, document, _, _ = imaging(django_user_model, text + '见结节，长径12mm。', name='scope-mixed-' + text)
    parent = document.facts.get(field_key='lesion.site')
    old = add_manual_clinical_field(patient, actor=patient.account, report_id=parent.clinical_report_id,
        entity_key=parent.entity_key, field_key='lesion.laterality', value={'code': code, 'raw': text},
        fragments=[{'page_number': parent.document_page.page_number, 'raw_text': text}],
        expected_report_source=report_source_token(parent.clinical_report))
    confirm(patient, parent)
    confirm(patient, old)
    pr, ps = expected(parent)
    cr, cs = expected(old)
    source = parent.source_fragments.get()
    start = source.ocr_block.text.index(text)
    before = list(Fact.objects.order_by('pk').values_list('pk', 'revision_number'))
    with pytest.raises(ValidationError):
        api().attest_whole_laterality(patient, actor=patient.account, fact_id=old.pk, parent_id=parent.pk,
            expected_revision=cr, expected_source=cs, expected_parent_revision=pr, expected_parent_source=ps,
            ranges=[{'member_key': 'whole', 'parent_fragment_id': source.pk, 'start_offset': start, 'end_offset': start + len(text)}],
            checked_original=True, confirm=True)
    assert list(Fact.objects.order_by('pk').values_list('pk', 'revision_number')) == before


def test_same_page_unrelated_clause_cannot_be_attested_as_member_source(django_user_model):
    patient, _, parent, old = fixture(django_user_model, 'scope-wrong-same-page')
    confirm(patient, parent)
    value, ranges = new_member(parent, old)
    source = old.laterality_scope_binding.ranges.get().parent_fragment
    start = source.ocr_block.text.index('淋巴结')
    ranges[0].update(start_offset=start, end_offset=start + 3)
    with pytest.raises(ValidationError):
        replace(patient, parent, old, value=value, ranges=ranges)
    old.refresh_from_db()
    assert old.revision_number == 0


def test_new_unselected_parent_sibling_and_author_history_are_part_of_undo_guard(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import add_manual_clinical_field
    patient, report, parent, old = fixture(django_user_model, 'scope-new-parent-dependency')
    confirm(patient, parent)
    event = replace(patient, parent, old, confirm_new=True)
    add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key=parent.entity_key,
        field_key='lesion.site', value={'text': '另一候选部位'}, fragments=[{'page_number': 1, 'raw_text': '另一候选部位'}],
        expected_report_source=report_source_token(report))
    with pytest.raises(FactConflict):
        undo(patient, event)


def test_replacement_event_and_actual_revision_links_are_immutable(django_user_model):
    patient, _, parent, old = fixture(django_user_model, 'scope-operation-immutable')
    confirm(patient, parent)
    event = replace(patient, parent, old, confirm_new=True)
    link = event.revision_links.first()
    event.before_state = {'forged': True}
    link.original_revision_id = event.new_fact.revisions.get().pk
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        link.save()
