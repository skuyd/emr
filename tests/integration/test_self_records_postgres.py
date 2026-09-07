from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.patients.access import change_membership
from apps.self_records.models import DailyRecord
from apps.self_records.services import RecordConflict, create_record, revise_record
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL test database')


def test_duplicate_creation_waits_for_commit_and_returns_same_identity(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'pg-daily-idempotency')
    pids, key = Queue(), uuid4()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = create_record(patient, actor, payload(), creation_key=key)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: create_record(patient, actor, payload(), creation_key=key), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        replay = future.result(timeout=15)
    assert first.created and not replay.created and first.record.pk == replay.record.pk
    assert DailyRecord.objects.filter(patient=patient).count() == 1


def test_two_authors_cannot_overwrite_the_same_revision(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'pg-daily-correction')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_record(patient, patient.account, record.pk,
                                 action='CORRECT', expected_revision=0, changes=payload(value='62')), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(RecordConflict):
            future.result(timeout=15)
    record.refresh_from_db()
    assert record.current_data['raw_value'] == '61' and record.revision_number == 1
    assert record.revisions.get().author_id == actor.pk


def test_waiting_write_reloads_committed_membership_revocation(django_user_model):
    _, patient, _, actor, membership = family(django_user_model, 'pg-daily-revoke')
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: create_record(patient, actor, payload(), creation_key=uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
    assert not DailyRecord.objects.filter(patient=patient).exists()


@pytest.mark.parametrize('deletion', ['actor', 'patient'])
def test_deletion_waits_for_original_author_write_and_then_denies_new_records(django_user_model, deletion):
    from apps.accounts.deletion import request_account_deletion
    from apps.patients.deletion import request_patient_deletion
    _, patient, _, actor, _ = family(django_user_model, 'pg-daily-deletion-' + deletion)
    pids = Queue()
    def delete():
        if deletion == 'actor':
            return request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        return request_patient_deletion(patient.pk, patient.account, document_dispatch=lambda _: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            created = create_record(patient, actor, payload(), creation_key=uuid4())
            blocker = backend_pid()
            future = pool.submit(thread_call, delete, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        future.result(timeout=15)
    record = DailyRecord.objects.get(pk=created.record.pk)
    assert record.created_by_id == actor.pk and record.patient_id == patient.pk
    with pytest.raises(PermissionDenied):
        create_record(patient, actor, payload(), creation_key=uuid4())


@pytest.mark.parametrize('route', ['list', 'detail'])
def test_rendered_read_is_discarded_after_independent_revocation(django_user_model, monkeypatch, route):
    from apps.self_records import views
    _, patient, client, actor, member = family(django_user_model, 'pg-daily-read-' + route)
    record = create_record(patient, actor, payload(notes='private daily boundary'), creation_key=uuid4()).record
    entered, release = Event(), Event()
    original_render = views.render
    def paused_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=15)
        return response
    monkeypatch.setattr(views, 'render', paused_render)
    path = '/self-records/' if route == 'list' else f'/self-records/{record.pk}/'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: client.get(path, {'patient': str(patient.pk)}))
        try:
            assert entered.wait(timeout=10)
            change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
        finally:
            release.set()
        response = future.result(timeout=15)
    assert response.status_code in {403, 404} and 'private daily boundary' not in response.content.decode()


@pytest.mark.parametrize('change', ['correct', 'delete', 'revoke'])
def test_generation_cannot_publish_after_selected_record_or_authorization_change(django_user_model, monkeypatch, change):
    from apps.exports import services
    from tests.documents.fakes import InMemoryObjectStore
    from tests.self_records.test_export_integration import selection
    _, patient, client, actor, member = family(django_user_model, 'pg-daily-build-' + change)
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    job = services.create_preview(patient, client.session.session_key, selection(record), actor=actor)
    services.request_generation(patient, client.session.session_key, job.pk, {'format': 'json'}, actor=actor, dispatch=lambda _: None)
    entered, release = Event(), Event()
    original_build = services.build_artifact
    def paused_build(*args, **kwargs):
        artifact = original_build(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=15)
        return artifact
    monkeypatch.setattr(services, 'build_artifact', paused_build)
    store = InMemoryObjectStore()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: services.generate_export(job.pk, store))
        try:
            assert entered.wait(timeout=10)
            if change == 'revoke':
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            else:
                revise_record(patient, actor, record.pk, action='CORRECT' if change == 'correct' else 'DELETE',
                              expected_revision=0, changes=payload(value='62') if change == 'correct' else None)
        finally:
            release.set()
        future.result(timeout=15)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and not store.objects


@pytest.mark.parametrize('target', ['export', 'share'])
def test_waiting_selection_cannot_bind_a_deleted_record(django_user_model, target):
    from apps.exports.errors import ExportInputError
    from apps.exports.models import ExportJob
    from apps.exports.services import create_preview
    from apps.patients.models import PatientShare
    from apps.patients.sharing import create_share
    from tests.self_records.test_export_integration import selection

    _, patient, client, actor, _ = family(django_user_model, 'pg-daily-bind-' + target)
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    pids = Queue()
    def create_selected():
        if target == 'export':
            return create_preview(patient, client.session.session_key, selection(record), actor=actor)
        return create_share(patient, patient.account, selection(record))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_record(patient, actor, record.pk, action='DELETE', expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, create_selected, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(ExportInputError):
            future.result(timeout=15)
    assert not ExportJob.objects.filter(patient=patient).exists()
    assert not PatientShare.objects.filter(patient=patient).exists()


@pytest.mark.parametrize('action', ['CORRECT', 'DELETE'])
def test_rendered_limited_share_is_discarded_after_committed_record_change(django_user_model, monkeypatch, action):
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent
    from apps.patients import share_views
    from apps.patients.sharing import create_share, exchange_share_token
    from tests.documents.test_detail_viewer import _patient
    from tests.self_records.test_export_integration import selection

    _, patient, _, actor, _ = family(django_user_model, 'pg-daily-shared-' + action)
    record = create_record(patient, actor, payload(notes='private selected before correction'), creation_key=uuid4()).record
    created = create_share(patient, patient.account, selection(record))
    reader, own = _patient(django_user_model, 'pg-daily-reader-' + action)
    assert reader.get('/shared/open/').status_code == 200
    exchange_share_token(created.token, own.account, reader.session.session_key)
    entered, release = Event(), Event()
    original_render = share_views.render
    def paused_render(request, template, *args, **kwargs):
        response = original_render(request, template, *args, **kwargs)
        if template == 'patients/shared_detail.html':
            assert b'private selected before correction' in response.content
            entered.set()
            assert release.wait(timeout=15)
        return response
    monkeypatch.setattr(share_views, 'render', paused_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: reader.get(f'/shared/{created.share.pk}/'))
        try:
            assert entered.wait(timeout=10)
            revise_record(patient, actor, record.pk, action=action, expected_revision=0,
                          changes=payload(value='63') if action == 'CORRECT' else None)
        finally:
            release.set()
        response = future.result(timeout=15)
    assert response.status_code == 410 and b'private selected before correction' not in response.content
    created.share.refresh_from_db()
    assert created.share.snapshot == {} and created.share.invalidated_at is not None
    assert AuditEvent.objects.filter(action='share_viewed', actor_hash=_hash('actor', own.account_id),
                                     patient_hash=_hash('patient', patient.pk), result='denied').exists()
