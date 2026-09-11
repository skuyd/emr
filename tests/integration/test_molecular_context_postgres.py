"""Actual committed molecular graph/author changes while review waits on locks."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import add_manual_clinical_field, revise_report
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.facts.molecular_factories import context_for
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_context import drug_graph, confirm
from tests.facts.test_molecular_replacement import request as replacement_request
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires an isolated synthetic PostgreSQL database')


@pytest.mark.parametrize('change', ['unselected_variant', 'new_assay_member', 'author_purge', 'editor_revoke'])
def test_waiting_whole_molecular_replacement_rejects_committed_changes(django_user_model, change):
    patient, _, report, fields, _, _, _ = drug_graph(django_user_model)
    _, collaborator = _patient(django_user_model, 'molecular-pg-actor')
    actor = collaborator.account
    member = PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    def add_metadata():
        return add_manual_clinical_field(patient, actor=actor, report_id=report.pk, entity_key='assay:a', field_key='assay.panel_name',
            value={'text': 'SYN panel'}, fragments=[{'page_number': 1, 'raw_text': '标本甲；检测甲；SYN panel'}],
            expected_report_source=report_source_token(report), source_role='PRIMARY_ASSAY_METADATA',
            entity_context=context_for(report, {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}))
    if change == 'author_purge':
        fields['panel'] = add_metadata()
    confirm(patient, fields)
    seed = fields['identity']
    seed.refresh_from_db()
    changes = replacement_request(patient, report, seed)
    source, number = effective_fact(seed)['current_source_token'], seed.revision_number
    if change == 'author_purge':
        job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    writer = actor if change == 'editor_revoke' else patient.account
    count = report.fields.count()
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'author_purge':
                assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
            elif change == 'editor_revoke':
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            elif change == 'new_assay_member':
                add_metadata()
            else:
                review(patient, fields['second'], 'REVOKE')
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(patient, seed.pk, actor=writer, action='REPLACE_CONTEXT',
                expected_revision=number, expected_source=source, checked_original=True, changes=changes), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        # Normal COMMIT includes any deferred author foreign keys.
        with pytest.raises(PermissionDenied if change == 'editor_revoke' else FactConflict):
            future.result(timeout=20)
    assert report.fields.count() == count + (change == 'new_assay_member')
    assert not report.fields.filter(revisions__after__has_key='context_replacement').exists()


def test_waiting_molecular_confirmation_cannot_escape_committed_report_exclusion(django_user_model):
    patient, _, report, fields, _, _, _ = drug_graph(django_user_model)
    fact = fields['metric']
    source = effective_fact(fact)['current_source_token']
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_report(patient, actor=patient.account, report_id=report.pk, action='EXCLUDE', expected_revision=0,
                expected_source=report_source_token(report))
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM',
                expected_revision=0, expected_source=source, checked_original=True), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(FactConflict):
            future.result(timeout=20)
    fact.refresh_from_db()
    assert effective_fact(fact)['status'] == 'EXCLUDED'
    assert not fact.revisions.filter(action='CONFIRM').exists()


@pytest.mark.parametrize('route', ['field', 'report', 'invalid_form'])
@pytest.mark.parametrize('change', ['new_assay_member', 'editor_revoke', 'document_trash'])
def test_actual_render_discards_body_after_other_connection_commits(django_user_model, monkeypatch, route, change):
    from apps.facts import views
    from apps.documents.lifecycle import move_to_trash
    patient, document, report, fields, _, _, _ = drug_graph(django_user_model)
    confirm(patient, fields)
    client, collaborator = _patient(django_user_model, 'molecular-pg-render')
    member = PatientMembership.objects.create(patient=patient, account=collaborator.account, role='EDITOR')
    original_render = views.render
    observed = []
    def commit_change():
        with transaction.atomic():
            if change == 'document_trash':
                move_to_trash(patient, document.pk, actor=patient.account)
            elif change == 'editor_revoke':
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            else:
                add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key='assay:a', field_key='assay.panel_name',
                    value={'text': 'SYN rendered new panel'}, fragments=[{'page_number': 1, 'raw_text': '标本甲；检测甲；SYN rendered new panel'}],
                    expected_report_source=report_source_token(report), source_role='PRIMARY_ASSAY_METADATA',
                    entity_context=context_for(report, {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}))
        observed.append('COMMITTED')
    def rendered(*args, **kwargs):
        response = original_render(*args, **kwargs)
        observed.append('BODY_RENDERED')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(thread_call, commit_change).result(timeout=20)
        return response
    monkeypatch.setattr(views, 'render', rendered)
    url = f'/facts/reports/{report.pk}/' if route == 'report' else f'/facts/{fields["metric"].pk}/'
    response = client.post(url, {'patient_id': str(patient.pk), 'action': 'CONFIRM'}) if route == 'invalid_form' else client.get(url)
    assert observed == ['BODY_RENDERED', 'COMMITTED']
    assert response.status_code == {'new_assay_member': 410, 'editor_revoke': 403, 'document_trash': 404}[change]
    assert '01.20' not in response.content.decode() and 'NM_SYN.2' not in response.content.decode()


@pytest.mark.parametrize('route', ['field', 'report'])
@pytest.mark.parametrize('post', [False, True])
@pytest.mark.parametrize('change', ['revoke', 'trash'])
def test_final_source_read_discards_body_after_another_connection_commits(django_user_model, monkeypatch, route, post, change):
    from apps.facts import pathology_views
    from apps.documents.lifecycle import move_to_trash
    from tests.facts.molecular_factories import graph
    _, patient, document, report, fields = graph(django_user_model)
    client, collaborator = _patient(django_user_model, 'molecular-final-read-pg')
    member = PatientMembership.objects.create(patient=patient, account=collaborator.account, role='EDITOR')
    name = 'report_material' if route == 'field' else 'review_reports'
    original = getattr(pathology_views, name)
    observed = []
    def commit_change():
        with transaction.atomic():
            if change == 'revoke':
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            else:
                move_to_trash(patient, document.pk, actor=patient.account)
        observed.append('COMMITTED')
    def reread(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append('READ')
        if len(observed) == 2:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(thread_call, commit_change).result(timeout=20)
        return result
    monkeypatch.setattr(pathology_views, name, reread)
    url = f'/facts/{fields["identity"].pk}/' if route == 'field' else f'/facts/reports/{report.pk}/'
    response = client.post(url, {'patient_id': str(patient.pk), 'action': 'CONFIRM'}) if post else client.get(url)
    assert observed == ['READ', 'READ', 'COMMITTED']
    assert 'NM_SYN.2' not in response.content.decode()
    assert response.status_code == (403 if change == 'revoke' else 404)
