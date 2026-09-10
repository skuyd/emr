"""Normal-COMMIT source changes must invalidate actual already rendered bodies."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest
from django.db import connection, connections

from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.patients.models import Patient
from tests.facts.test_source_read_freshness import source_page, fetch_page
from tests.facts.test_scoped_laterality import confirm


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.mark.parametrize('route', ['scope_get', 'scope_invalid_post', 'lesion_detail'])
@pytest.mark.parametrize('change', [False, True])
def test_source_commit_between_render_and_release(django_user_model, monkeypatch, record_property, route, change):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires an isolated PostgreSQL test database')
    assert not connection.in_atomic_block
    patient, parent, client, views, url, target, method, unchanged_status = source_page(
        django_user_model, route, f'pg-read-freshness-{route}-{change}')
    before = effective_fact(parent)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_backend_pid()')
        reader_pid = cursor.fetchone()[0]
    original, rendered = views.render, []
    def mutation():
        try:
            assert not connection.in_atomic_block
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                writer_pid = cursor.fetchone()[0]
            assert reader_pid != writer_pid
            confirm(Patient.objects.get(pk=patient.pk), Fact.objects.get(pk=parent.pk), 'REVOKE')
            assert not connection.in_atomic_block
            return writer_pid
        finally:
            connections.close_all()
    def render_then_commit(request, template, context, **kwargs):
        response = original(request, template, context, **kwargs)
        if template == target:
            rendered.append(hashlib.sha256(response.content).hexdigest())
            if change:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    record_property('writer_pid', pool.submit(mutation).result(timeout=30))
                current = effective_fact(Fact.objects.get(pk=parent.pk))
                assert current['revision_number'] == before['revision_number'] + 1
                assert not current['usable']
                record_property('writer_commit_observed', True)
        return response
    monkeypatch.setattr(views, 'render', render_then_commit)
    response = fetch_page(client, patient, method, url)
    assert rendered
    released = hashlib.sha256(response.content).hexdigest()
    record_property('reader_pid', reader_pid)
    record_property('render_sha256', rendered[0])
    record_property('released_sha256', released)
    record_property('status', response.status_code)
    assert response.status_code == (409 if change else unchanged_status)
    assert (released != rendered[0]) is change


@pytest.mark.parametrize('change', ['revoke', 'downgrade'])
def test_permission_commit_after_final_source_read(django_user_model, monkeypatch, record_property, change):
    from apps.facts import read_guards
    from apps.patients.access import change_membership
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    if connection.vendor != 'postgresql':
        pytest.skip('Requires an isolated PostgreSQL test database')
    assert not connection.in_atomic_block
    patient, _, client, _, url, _, method, _ = source_page(
        django_user_model, 'scope_invalid_post', 'final-permission-pg-' + change)
    _, own = _patient(django_user_model, 'final-permission-pg-editor-' + change)
    member = PatientMembership.objects.create(patient=patient, account=own.account, role='EDITOR')
    client.force_login(own.account)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_backend_pid()')
        reader_pid = cursor.fetchone()[0]
    def mutation():
        try:
            assert not connection.in_atomic_block
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                writer_pid = cursor.fetchone()[0]
            assert reader_pid != writer_pid
            current_patient = Patient.objects.get(pk=patient.pk)
            change_membership(current_patient, current_patient.account, member.pk,
                expected_revision=member.revision, revoke=change == 'revoke',
                role='VIEWER' if change == 'downgrade' else None)
            assert not connection.in_atomic_block
            return writer_pid
        finally:
            connections.close_all()
    original, snapshots = read_guards.source_snapshot, []
    def read_then_commit(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshots.append(snapshot)
        if len(snapshots) == 2:
            with ThreadPoolExecutor(max_workers=1) as pool:
                record_property('writer_pid', pool.submit(mutation).result(timeout=30))
            current = PatientMembership.objects.get(pk=member.pk)
            assert current.revision == member.revision + 1
            assert current.revoked_at is not None if change == 'revoke' else current.role == 'VIEWER'
            record_property('writer_commit_observed', True)
        return snapshot
    monkeypatch.setattr(read_guards, 'source_snapshot', read_then_commit)
    response = fetch_page(client, patient, method, url)
    record_property('reader_pid', reader_pid)
    assert len(snapshots) == 2
    assert response.status_code in {403, 409}
    assert b'members-TOTAL_FORMS' not in response.content
