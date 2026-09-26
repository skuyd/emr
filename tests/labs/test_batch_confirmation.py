import uuid

import pytest
from django.test import Client

from apps.labs.models import ObservationRevision, ReportAssociation
from apps.labs.reports import correct_report, decide_relation, report_relations
from apps.labs.revisions import effective_observation, revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db
PATH = '/labs/reports/batch-confirmation/'


def preview(client):
    response = client.get(PATH)
    assert response.status_code == 200
    return response.context['reports']


def payload(patient, reports, *, operation_id=None):
    return {'patient_id': str(patient.pk), 'report': [item['token'] for item in reports],
            'operation_id': str(operation_id or uuid.uuid4())}


def test_preview_groups_duplicate_sources_without_expanding_to_equal_other_reports(django_user_model):
    client, patient = _patient(django_user_model, 'batch-preview')
    _, first, _ = report(patient)
    _, duplicate, _ = report(patient)
    _, other, _ = report(patient, number='B200')
    groups = preview(client)
    assert sorted(item['pending_count'] for item in groups) == [1, 2]
    selected = next(item for item in groups if item['pending_count'] == 2)
    response = client.post(PATH, payload(patient, [selected]))
    assert response.status_code == 200
    assert response.context['result']['confirmed_count'] == 2
    assert set(ObservationRevision.objects.values_list('observation_id', flat=True)) == {first.pk, duplicate.pk}
    assert not other.revisions.exists()
    assert report_relations(patient)[0].state == 'AUTO'


def test_single_and_multiple_selected_reports_keep_values_and_per_item_audit(django_user_model):
    client, patient = _patient(django_user_model, 'batch-multiple')
    _, first, first_unit = report(patient)
    _, second, second_unit = report(patient, number='B200', value='7')
    before = [first_unit.automatic, second_unit.automatic]
    response = client.post(PATH, payload(patient, preview(client)))
    assert response.status_code == 200
    assert response.context['result']['confirmed_count'] == 2
    assert response.context['result']['report_count'] == 2
    for row, value in [(first, '5'), (second, '7')]:
        row.refresh_from_db()
        event, = row.revisions.all()
        assert event.action == 'CONFIRM' and event.author_id == patient.account_id
        assert event.before['raw_value'] == event.after['raw_value'] == value
        assert effective_observation(row).review_state == 'CONFIRM'
    first_unit.refresh_from_db()
    second_unit.refresh_from_db()
    assert [first_unit.automatic, second_unit.automatic] == before
    assert [first_unit.revision_number, second_unit.revision_number] == [0, 0]
    from apps.operations.models import AuditEvent
    assert AuditEvent.objects.filter(action='lab_revised', reason_code='confirm', result='succeeded').count() == 2


def test_mixed_states_skip_errors_and_report_conflicts_but_confirm_normal_pending(django_user_model):
    client, patient = _patient(django_user_model, 'batch-mixed')
    _, pending, _ = report(patient, number='P')
    _, confirmed, _ = report(patient, number='C')
    _, error, _ = report(patient, number='E')
    _, conflict, _ = report(patient, number='X')
    _, conflicting_source, _ = report(patient, number='X', value='6')
    revise_observation(patient.account, confirmed.pk, action='CONFIRM', changes={}, expected_revision=0)
    revise_observation(patient.account, error.pk, action='REPORT_ERROR', changes={}, expected_revision=0)
    revise_observation(patient.account, error.pk, action='DEFER', changes={}, expected_revision=1)
    groups = preview(client)
    assert sum(item['pending_count'] for item in groups) == 1
    assert sum(item['confirmed_count'] for item in groups) == 1
    assert sum(item['skipped_count'] for item in groups) == 3
    response = client.post(PATH, payload(patient, groups))
    result = response.context['result']
    assert result['confirmed_count'] == 1 and result['already_confirmed_count'] == 1
    assert result['skipped_count'] == result['remaining_count'] == 3
    assert pending.revisions.filter(action='CONFIRM').count() == 1
    assert confirmed.revisions.count() == 1
    assert error.revisions.count() == 2
    assert not conflict.revisions.exists() and not conflicting_source.revisions.exists()
    error.refresh_from_db()
    assert effective_observation(error).reported_error
    assert '识别有误' in response.content.decode() and '报告归属冲突' in response.content.decode()


def test_missing_unit_and_low_confidence_remain_quality_limited_after_confirmation(django_user_model):
    client, patient = _patient(django_user_model, 'batch-quality')
    _, row, _ = report(patient)
    row.raw_unit = ''
    row.save(update_fields=['raw_unit'])
    row.evidence.confidence = '.4'
    row.evidence.save(update_fields=['confidence'])
    response = client.post(PATH, payload(patient, preview(client)))
    assert response.status_code == 200 and response.context['result']['confirmed_count'] == 1
    row.refresh_from_db()
    effective = effective_observation(row)
    assert effective.raw_unit == '' and effective.resolved_issues == []
    from apps.labs.validation import validate_observation
    assert {'unit_unknown', 'recognition_uncertain'} <= {issue['code'] for issue in validate_observation(effective)}


def test_repeat_submission_returns_original_counts_without_another_revision(django_user_model):
    client, patient = _patient(django_user_model, 'batch-repeat')
    _, row, _ = report(patient)
    data = payload(patient, preview(client))
    first = client.post(PATH, data)
    second = client.post(PATH, data)
    assert first.status_code == second.status_code == 200
    assert first.context['result'] == second.context['result']
    assert second.context['result']['confirmed_count'] == 1
    assert row.revisions.count() == 1
    changed = payload(patient, preview(client), operation_id=data['operation_id'])
    assert client.post(PATH, changed).status_code == 409


@pytest.mark.parametrize('state', ['AUTOMATIC', 'CONFIRM', 'REPORT_ERROR'])
def test_two_valid_preview_tokens_for_same_report_are_rejected_without_miscount(django_user_model, state):
    from unittest.mock import patch
    from apps.labs.models import LabConfirmationBatch

    client, patient = _patient(django_user_model, 'batch-duplicate-scope-' + state)
    _, row, _ = report(patient)
    if state != 'AUTOMATIC':
        revise_observation(patient.account, row.pk, action=state, changes={}, expected_revision=0)
    with patch('django.core.signing.time.time', return_value=1801000000):
        first, = preview(client)
    with patch('django.core.signing.time.time', return_value=1801000002):
        second, = preview(client)
    assert first['token'] != second['token'] and first['key'] == second['key']
    response = client.post(PATH, payload(patient, [first, second]))
    assert response.status_code == 400
    assert row.revisions.count() == (0 if state == 'AUTOMATIC' else 1)
    assert not LabConfirmationBatch.objects.exists()


@pytest.mark.parametrize('change', ['result', 'report', 'relation', 'parse', 'delete'])
def test_stale_preview_rejects_entire_submission_before_any_confirmation(django_user_model, change):
    client, patient = _patient(django_user_model, 'batch-stale-' + change)
    _, first, unit = report(patient)
    document, second, _ = report(patient, number='B200')
    if change == 'relation':
        report(patient)
    data = payload(patient, preview(client))
    if change == 'result':
        revise_observation(patient.account, second.pk, action='CORRECT', changes={'raw_value': '7'}, expected_revision=0)
    elif change == 'report':
        correct_report(patient, patient.account, unit.pk, {'institution': '更正医院'}, expected_revision=0,
            source_evidence={'page_number': 1, 'polygon': [[.1, .01], [.9, .01], [.9, .02], [.1, .02]]},
            rationale='对照原件', operation_id='batch-stale-report')
    elif change == 'relation':
        association = report_relations(patient)[0]
        decide_relation(patient, patient.account, association.pk, 'UNDO', expected_revision=association.revision_number,
                        rationale='不是同一报告', operation_id='batch-stale-relation')
    elif change == 'parse':
        second.parsing_version.active = False
        second.parsing_version.save(update_fields=['active'])
    else:
        from django.utils import timezone
        document.deleted_at = timezone.now()
        document.save(update_fields=['deleted_at'])
    response = client.post(PATH, data)
    assert response.status_code == 409
    assert not ObservationRevision.objects.filter(action='CONFIRM').exists()


def test_relation_change_during_preview_cannot_confirm_items_presented_as_skipped(django_user_model):
    from unittest.mock import patch
    from apps.labs import batch_confirmation
    from apps.labs.models import LabConfirmationBatch

    client, patient = _patient(django_user_model, 'batch-preview-relation-change')
    report(patient, value='5')
    report(patient, value='6')
    association, = report_relations(patient)
    assign = batch_confirmation.assign_report_groups

    def resolve_between_reads(current_patient, rows):
        assert all(row.report_conflict for row in rows)
        decide_relation(current_patient, current_patient.account, association.pk, 'DIFFERENT',
                        expected_revision=association.revision_number,
                        rationale='独立报告', operation_id='preview-relation-change')
        assign(current_patient, rows)

    with patch.object(batch_confirmation, 'assign_report_groups', resolve_between_reads):
        groups = preview(client)
    assert sum(group['pending_count'] for group in groups) == 0
    assert sum(group['skipped_count'] for group in groups) == 2
    response = client.post(PATH, payload(patient, groups))
    assert response.status_code == 409
    assert not ObservationRevision.objects.exists()
    assert not LabConfirmationBatch.objects.exists()


def test_changes_to_unselected_independent_report_do_not_block_selected_report(django_user_model):
    client, patient = _patient(django_user_model, 'batch-independent')
    _, first, _ = report(patient)
    _, second, _ = report(patient, number='B200')
    selected = next(item for item in preview(client) if any(entry['row'].pk == first.pk for entry in item['items']))
    revise_observation(patient.account, second.pk, action='CORRECT', changes={'raw_value': '7'}, expected_revision=0)
    response = client.post(PATH, payload(patient, [selected]))
    assert response.status_code == 200 and response.context['result']['confirmed_count'] == 1


@pytest.mark.parametrize('bad', ['empty', 'tampered', 'operation', 'foreign'])
def test_invalid_or_foreign_selection_never_writes(django_user_model, bad):
    client, patient = _patient(django_user_model, 'batch-invalid-' + bad)
    report(patient)
    data = payload(patient, preview(client))
    if bad == 'empty':
        data['report'] = []
    elif bad == 'tampered':
        data['report'] = [data['report'][0] + 'x']
    elif bad == 'operation':
        data['operation_id'] = ''
    else:
        other_client, other = _patient(django_user_model, 'batch-foreign')
        report(other)
        data['report'] = payload(other, preview(other_client))['report']
    assert client.post(PATH, data).status_code in {400, 403}
    assert not ObservationRevision.objects.exists()


def test_viewer_cannot_confirm_and_revocation_also_blocks_repeat(django_user_model):
    from apps.patients.models import PatientMembership
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'batch-owner')
    member_client, member_patient = _patient(django_user_model, 'batch-member')
    membership = PatientMembership.objects.create(patient=patient, account=member_patient.account, role='EDITOR')
    _, row, _ = report(patient)
    response = member_client.get(PATH, {'patient': str(patient.pk)})
    assert response.status_code == 200
    data = payload(patient, response.context['reports'])
    assert member_client.post(PATH, data).status_code == 200
    membership.role = 'VIEWER'
    membership.save(update_fields=['role'])
    assert member_client.post(PATH, data).status_code == 403
    response = member_client.get(PATH, {'patient': str(patient.pk)})
    assert '确认所选报告' not in response.content.decode()
    membership.revoked_at = timezone.now()
    membership.save(update_fields=['revoked_at'])
    assert member_client.post(PATH, data).status_code in {403, 404}
    assert row.revisions.count() == 1


def test_batch_post_requires_csrf(django_user_model):
    _, patient = _patient(django_user_model, 'batch-csrf')
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    assert client.post(PATH, {'patient_id': patient.pk}).status_code == 403


def test_plain_confirmation_does_not_invalidate_report_association(django_user_model):
    _, patient = _patient(django_user_model, 'batch-relation-confirm')
    _, row, _ = report(patient)
    report(patient)
    association = report_relations(patient)[0]
    assert association.state == 'AUTO'
    revise_observation(patient.account, row.pk, action='CONFIRM', changes={}, expected_revision=0)
    assert report_relations(patient)[0].state == 'AUTO'
    assert ReportAssociation.objects.count() == 1


def test_continuation_is_confirmed_with_its_main_report_without_changing_its_borrowed_time(django_user_model):
    from tests.labs.test_report_readmodels import continuation_pair
    client, patient = _patient(django_user_model, 'batch-continuation')
    _, rows, units = continuation_pair(patient)
    before = [unit.automatic for unit in units]
    groups = preview(client)
    assert len(groups) == 1 and groups[0]['pending_count'] == 2
    response = client.post(PATH, payload(patient, groups))
    assert response.status_code == 200 and response.context['result']['confirmed_count'] == 2
    assert all(row.revisions.filter(action='CONFIRM').count() == 1 for row in rows)
    assert len(preview(client)) == 1
    for unit, original in zip(units, before):
        unit.refresh_from_db()
        assert unit.automatic == original and unit.revision_number == 0


@pytest.mark.parametrize('conflict', ['result', 'report'])
def test_inherited_reparse_conflicts_remain_unconfirmed(django_user_model, conflict):
    from dataclasses import replace
    from apps.labs.reports import _from_snapshot
    from tests.labs.test_report_revision_versions import next_report_version, SOURCE
    client, patient = _patient(django_user_model, 'batch-inherited-' + conflict)
    _, original, unit = report(patient)
    if conflict == 'result':
        revise_observation(patient.account, original.pk, action='CORRECT', changes={'raw_value': '6'}, expected_revision=0)
        current, = next_report_version(unit)
        current.observations.update(raw_value='7')
    else:
        correct_report(patient, patient.account, unit.pk, {'institution': '核对后的医院'}, expected_revision=0,
                       source_evidence=SOURCE, rationale='原件医院', operation_id='batch-inherited')
        current, = next_report_version(unit, (replace(_from_snapshot(unit.automatic), report_number='CHANGED'),))
    groups = preview(client)
    assert groups[0]['skipped_count'] == 1 and groups[0]['pending_count'] == 0
    response = client.post(PATH, payload(patient, groups))
    assert response.context['result']['confirmed_count'] == 0
    assert response.context['result']['remaining_count'] == 1
    assert not ObservationRevision.objects.filter(action='CONFIRM').exists()
    assert '全部已确认' not in response.content.decode()


def test_same_report_with_skipped_item_is_never_presented_as_complete(django_user_model):
    from copy import copy
    client, patient = _patient(django_user_model, 'batch-partial')
    _, first, _ = report(patient)
    error = copy(first)
    error.pk, error.reading_order = uuid.uuid4(), first.reading_order + 1
    error.save(force_insert=True)
    revise_observation(patient.account, error.pk, action='REPORT_ERROR', changes={}, expected_revision=0)
    groups = preview(client)
    assert len(groups) == 1 and groups[0]['pending_count'] == groups[0]['skipped_count'] == 1
    response = client.post(PATH, payload(patient, groups))
    assert response.context['result']['confirmed_count'] == response.context['result']['remaining_count'] == 1
    assert '本报告未全部确认' in response.content.decode()
    assert first.revisions.filter(action='CONFIRM').count() == 1
    assert error.revisions.count() == 1


def test_authority_is_rechecked_after_waiting_for_document_lock(django_user_model, monkeypatch):
    from apps.labs import batch_confirmation
    from django.core.exceptions import PermissionDenied
    client, patient = _patient(django_user_model, 'batch-recheck-account')
    _, row, _ = report(patient)
    tokens = [item['token'] for item in preview(client)]
    original_lock = batch_confirmation.lock_document_aggregate

    def deactivate_after_lock(*args, **kwargs):
        locked = original_lock(*args, **kwargs)
        django_user_model.objects.filter(pk=patient.account_id).update(is_active=False)
        return locked

    monkeypatch.setattr(batch_confirmation, 'lock_document_aggregate', deactivate_after_lock)
    with pytest.raises(PermissionDenied):
        batch_confirmation.confirm_reports(patient, patient.account, tokens, operation_id=uuid.uuid4())
    assert not row.revisions.exists()
