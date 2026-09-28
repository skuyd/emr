import pytest

from apps.labs.report_workspace import report_workspace
from apps.labs.reports import correct_report, report_relations
from apps.labs.revisions import revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def test_workspace_enumerates_zero_observation_report_and_joined_zero_observation_source(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-zero')
    _, first, first_unit = report(patient)
    _, continuation, continuation_unit = report(patient)
    continuation.delete()
    _, empty, empty_unit = report(patient, number='B200')
    empty.delete()

    workspace = report_workspace(patient)

    assert len(workspace['reports']) == 2
    joined = next(item for item in workspace['reports'] if first_unit.pk in {unit.pk for unit in item['units']})
    zero = next(item for item in workspace['reports'] if empty_unit.pk in {unit.pk for unit in item['units']})
    assert {unit.pk for unit in joined['units']} == {first_unit.pk, continuation_unit.pk}
    assert {source['unit'].pk for source in joined['sources']} == {first_unit.pk, continuation_unit.pk}
    assert [row.pk for row in joined['rows']] == [first.pk]
    assert zero['rows'] == () and len(zero['sources']) == 1


def test_workspace_fingerprint_tracks_current_report_but_not_independent_report(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-fingerprint')
    _, first, first_unit = report(patient)
    _, second, second_unit = report(patient, number='B200')
    initial = report_workspace(patient)
    fingerprints = {item['key']: item['fingerprint'] for item in initial['reports']}
    first_key = next(item['key'] for item in initial['reports'] if first_unit in item['units'])
    second_key = next(item['key'] for item in initial['reports'] if second_unit in item['units'])

    revise_observation(patient.account, first.pk, action='CORRECT', changes={'raw_value': '7'}, expected_revision=0)
    changed = {item['key']: item['fingerprint'] for item in report_workspace(patient)['reports']}
    assert changed[first_key] != fingerprints[first_key]
    assert changed[second_key] == fingerprints[second_key]

    correct_report(patient, patient.account, first_unit.pk, {'institution': '更正合成医院'},
                   expected_revision=0,
                   source_evidence={'page_number': 1, 'polygon': [[.1, .01], [.9, .01], [.9, .02], [.1, .02]]},
                   rationale='核对原件医院', operation_id='workspace-fingerprint')
    corrected = {item['key']: item['fingerprint'] for item in report_workspace(patient)['reports']}
    assert corrected[first_key] != changed[first_key]
    assert corrected[second_key] == changed[second_key]
    assert report_relations(patient) == ()


def test_workspace_opens_first_unconfirmed_report(django_user_model):
    import uuid

    from apps.labs.report_workspace import submit_report_workspace

    _, patient = _patient(django_user_model, 'workspace-priority')
    report(patient, number='A100')
    report(patient, number='B200')
    first, second = report_workspace(patient)['reports']
    submit_report_workspace(patient, patient.account, first['key'], first['token'], uuid.uuid4(), {}, confirm=True)
    assert report_workspace(patient)['current']['key'] == second['key']


def test_workspace_navigation_follows_source_creation_order(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-navigation-order')
    _, _, first_unit = report(patient, number='A100')
    _, _, second_unit = report(patient, number='B200')
    first_unit.source_key = 'z-first-report'
    first_unit.save(update_fields=['source_key'])
    second_unit.source_key = 'a-second-report'
    second_unit.save(update_fields=['source_key'])

    assert [item['units'][0].pk for item in report_workspace(patient)['reports']] == [
        first_unit.pk, second_unit.pk]
