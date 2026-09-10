"""Actual rendered responses and downloaded bytes cross committed source changes."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

import pytest
from django.db import connection, transaction

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.lesions.services import rename_lesion
from apps.patients.models import PatientMembership
from apps.patients.sharing import create_share
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_lesion_output_bindings import authenticated
from tests.exports.test_lesion_portable_formats import prepared
from tests.exports.test_lesion_selected_material import ids
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.patients.test_family_shares import exchange


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated lesion output PostgreSQL database')


@pytest.mark.parametrize('change', ['hidden_scope_parent', 'historical_name_author_purge'])
def test_rendered_share_is_discarded_after_original_dependency_commit(django_user_model, monkeypatch, change):
    from apps.patients import share_views

    if change == 'hidden_scope_parent':
        patient, report, parent, child = fixture(django_user_model, 'pg-rendered-hidden-scope')
        confirm(patient, parent)
        confirm(patient, child)
        scope = {'document_ids': [str(report.document_id)], 'clinical_field_ids': [str(child.pk)], 'sections': ['imaging']}
        marker = '双肺门'
        def mutate():
            confirm(patient, parent, action='REVOKE')
    else:
        patient, _, _, scope = prepared(django_user_model, 'pg-rendered-lesion-author')
        _, own = _patient(django_user_model, 'pg-rendered-lesion-old-author')
        editor = own.account
        PatientMembership.objects.create(patient=patient, account=editor, role='EDITOR')
        lesion = patient.lesions.get()
        rename_lesion(patient, actor=editor, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='历史中间名称')
        lesion.refresh_from_db()
        marker = '当前名称保留标记'
        rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name=marker)
        def mutate():
            deletion = request_account_deletion(editor.pk, document_dispatch=lambda *_: None, account_dispatch=lambda *_: None)
            assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
            assert not editor.__class__.objects.filter(pk=editor.pk).exists()
            assert lesion.revisions.filter(operation__author__isnull=True).exists()
    made = create_share(patient, patient.account, scope)
    reader, _ = _patient(django_user_model, 'pg-rendered-lesion-reader-' + change)
    identity = exchange(reader, made.token)
    entered, release, pids = Event(), Event(), Queue()
    original_render = share_views.render
    def pause_after_render(request, template, *args, **kwargs):
        response = original_render(request, template, *args, **kwargs)
        if template == 'patients/shared_detail.html':
            assert marker.encode() in response.content
            if change == 'hidden_scope_parent':
                assert '纵隔'.encode() not in response.content
            entered.set()
            assert release.wait(timeout=20)
        return response
    monkeypatch.setattr(share_views, 'render', pause_after_render)
    main_pid = backend_pid()
    assert not connection.in_atomic_block
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: reader.get(f'/shared/{identity}/'), pids)
        try:
            assert pids.get(timeout=10) != main_pid
            assert entered.wait(timeout=15)
            with transaction.atomic():
                mutate()
            assert not connection.in_atomic_block  # Actual COMMIT precedes final HTTP access check.
        finally:
            release.set()
        response = future.result(timeout=20)
    assert response.status_code == 410 and marker.encode() not in response.content
    made.share.refresh_from_db()
    assert made.share.invalidated_at is not None and made.share.snapshot == {}


@pytest.mark.parametrize('change', ['selected_parent', 'unselected_other_report_field'])
def test_actual_json_download_stops_after_committed_private_dependency_change(django_user_model, monkeypatch, change):
    from apps.exports import services, views
    from apps.operations.models import AuditEvent
    from tests.documents.fakes import InMemoryObjectStore

    patient, reports, _, scope = prepared(django_user_model, 'pg-lesion-stream-' + change)
    scope['document_ids'] = [str(reports[0].document_id)]
    scope['clinical_field_ids'] = ids(reports[0], 'lesion.site', 'lesion.dimensions')
    client = authenticated(patient)
    job = services.create_preview(patient, client.session.session_key, scope, actor=patient.account)
    assert str(reports[1].pk) not in str(job.snapshot['lesion_observations'])
    services.request_generation(patient, client.session.session_key, job.pk, {'format': 'json'},
                                dispatch=lambda *_: None, actor=patient.account)
    store = InMemoryObjectStore()
    services.generate_export(job.pk, store)
    monkeypatch.setattr(views, 'get_object_store', lambda: store)
    response = client.get(f'/visit/{job.pk}/download/', {'patient': str(patient.pk)})
    assert response.status_code == 200
    response.block_size = 256
    stream = iter(response.streaming_content)
    assert len(next(stream)) == 256
    field = (reports[0].fields.get(field_key='lesion.site') if change == 'selected_parent'
             else reports[1].fields.get(field_key='lesion.suvmax'))
    pids, main_pid = Queue(), backend_pid()
    def mutate():
        with transaction.atomic():
            confirm(patient, field, action='REVOKE')
        assert not connection.in_atomic_block
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, mutate, pids)
        assert pids.get(timeout=10) != main_pid
        future.result(timeout=20)
    try:
        assert b''.join(stream) == b''
    finally:
        response.close()
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}
    assert AuditEvent.objects.filter(action='export_downloaded', result='denied').count() == 1
