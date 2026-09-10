"""Independent committed changes across selected-output delivery boundaries."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
import io

from django.db import connection, transaction
import pytest

from apps.cloud_imaging import shared_views
from apps.core.streams import GuardedStream
from apps.exports import services, views as export_views
from apps.exports.errors import ExportInputError, ExportUnavailable
from apps.patients import share_views
from apps.patients.sharing import create_share
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cloud_imaging.test_controlled_open import payload
from tests.cloud_imaging.test_selected_output import source_selection
from tests.cloud_imaging.test_source_services import SECOND_URL, _decide
from tests.documents.test_detail_viewer import _patient
from tests.integration.test_cloud_open_postgres import purge
from tests.integration.test_cloud_sources_postgres import case, manual
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_shares import exchange

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated selected cloud output PostgreSQL database')


def setup(django_user_model, marker):
    owner, patient, _, actor, _, document, page, store = case(django_user_model, marker)
    source = _decide(patient, manual(patient, actor, document, page), 'CONFIRM')
    assert owner.get('/visit/').status_code == 200
    return owner, patient, actor, document, source, store


def change_source(patient, source, actor, change):
    assert not connection.in_atomic_block
    if change == 'revision':
        _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
    else:
        purge(actor)
        source.refresh_from_db()
        assert source.created_by_id is None
    assert not connection.in_atomic_block


@pytest.mark.parametrize('change', ['revision', 'author_purge'])
@pytest.mark.parametrize('consumer', ['export', 'share'])
def test_creation_waits_for_uncommitted_source_or_author_change(django_user_model, change, consumer):
    owner, patient, actor, _, source, _ = setup(django_user_model, 'create-' + change + consumer)
    selection = source_selection(source, sections=[], cloud_source_tokens={str(source.pk): payload(patient, source)['expected_source']})
    if consumer == 'share':
        assert create_share(patient, patient.account, selection).share.cloud_sources.exists()
    action = (lambda: services.create_preview(patient, owner.session.session_key, selection, actor=patient.account)) if consumer == 'export' else (
        lambda: create_share(patient, patient.account, selection))
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'revision':
                _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
            else:
                purge(actor)
            blocker = backend_pid()
            pending = pool.submit(thread_call, action, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(ExportInputError):
            pending.result(timeout=20)


@pytest.mark.parametrize('phase', ['selection_get', 'selection_invalid', 'share_selection', 'share_selection_invalid', 'detail', 'notice', 'shared_invalid', 'redirect', 'scripted'])
@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_render_and_navigation_discard_after_normal_commit(django_user_model, monkeypatch, phase, change):
    owner, patient, actor, _, source, _ = setup(django_user_model, phase + change)
    if phase.startswith('selection'):
        target, seam = export_views, 'render'
        action = lambda: owner.post('/visit/', {'mode':'invalid'}) if phase == 'selection_invalid' else owner.get('/visit/')
    elif phase.startswith('share_selection'):
        target, seam = share_views, 'render'
        action = lambda: (owner.post(f'/patients/{patient.pk}/shares/', {'expires_in_hours':'invalid'})
            if phase == 'share_selection_invalid' else owner.get(f'/patients/{patient.pk}/shares/'))
    else:
        reader, _ = _patient(django_user_model, 'pg-output-reader-' + phase + change)
        created = create_share(patient, patient.account, source_selection(source, sections=[]))
        share_id = exchange(reader, created.token)
        data = payload(patient, source); data.pop('patient_id')
        if phase == 'shared_invalid':data['expected_revision']='invalid'
        target, seam = (share_views, 'render') if phase == 'detail' else (shared_views, 'render' if phase in {'notice','shared_invalid'} else '_external_redirect')
        action = (lambda: reader.get(f'/shared/{share_id}/')) if phase == 'detail' else (
            (lambda: reader.get(f'/shared/{share_id}/cloud-imaging/{source.pk}/visit/')) if phase == 'notice' else (
            lambda: reader.post(f'/shared/{share_id}/cloud-imaging/{source.pk}/open/', data,
                **({'HTTP_X_CLOUD_OPEN':'navigate'} if phase == 'scripted' else {}))))
    entered, resume = Event(), Event()
    original = getattr(target, seam)
    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert resume.wait(20)
        return result
    monkeypatch.setattr(target, seam, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(thread_call, action)
        try:
            assert entered.wait(20)
            change_source(patient, source, actor, change)
        finally:
            resume.set()
        response = pending.result(timeout=20)
    assert response.status_code in (409, 410)
    assert 'Location' not in response and b'SYNTHETIC_FIRST' not in response.content
    assert 'expected_source' not in response.content.decode()


@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_worker_build_commit_invalidates_nonempty_artifact(django_user_model, monkeypatch, change):
    owner, patient, actor, _, source, store = setup(django_user_model, 'build-' + change)
    key = owner.session.session_key
    job = services.create_preview(patient, key, source_selection(source), actor=patient.account)
    services.request_generation(patient, key, job.pk, {'format':'json'}, dispatch=lambda _:None, actor=patient.account)
    entered, resume = Event(), Event()
    original = services.build_artifact
    def paused(*args, **kwargs):
        artifact = original(*args, **kwargs)
        assert artifact.byte_size > 100
        entered.set()
        assert resume.wait(20)
        return artifact
    monkeypatch.setattr(services, 'build_artifact', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(thread_call, lambda: services.generate_export(job.pk, store))
        try:
            assert entered.wait(20)
            change_source(patient, source, actor, change)
        finally:
            resume.set()
        pending.result(timeout=20)
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending
    assert services.cleanup_export(job.pk, store)


@pytest.mark.parametrize('inside_read', [False, True])
@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_each_nonempty_stream_chunk_checks_real_commit(django_user_model, inside_read, change):
    owner, patient, actor, _, source, store = setup(django_user_model, 'stream-' + change + str(inside_read))
    key = owner.session.session_key
    job = services.create_preview(patient, key, source_selection(source), actor=patient.account)
    services.request_generation(patient, key, job.pk, {'format':'json'}, dispatch=lambda _:None, actor=patient.account)
    services.generate_export(job.pk, store)
    job.refresh_from_db(); assert job.status == 'READY' and job.byte_size > 100
    artifact = services.download_export(patient, key, job.pk, store, actor=patient.account)
    content = artifact.stream.read(); artifact.close()
    entered, resume = Event(), Event()
    class Buffer(io.BytesIO):
        def read(self, size=-1):
            value = super().read(size)
            if inside_read and self.tell() > 8:
                entered.set(); assert resume.wait(20)
            return value
    raw = Buffer(content)
    stream = GuardedStream(raw, lambda: services.validate_export_stream(job.pk, patient.account, key))
    assert stream.read(8) == content[:8]
    if inside_read:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(thread_call, lambda: stream.read(8))
            try:
                assert entered.wait(20)
                change_source(patient, source, actor, change)
            finally:
                resume.set()
            assert pending.result(timeout=20) == b''
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(thread_call, lambda: change_source(patient, source, actor, change)).result(timeout=20)
        assert stream.read(8) == b''
    assert stream.denied and raw.closed


@pytest.mark.parametrize('phase', ['put', 'promote', 'download'])
def test_storage_io_serializes_real_writer_then_fences_next_delivery(django_user_model, monkeypatch, phase):
    owner, patient, _, _, source, store = setup(django_user_model, 'storage-' + phase)
    key = owner.session.session_key
    job = services.create_preview(patient, key, source_selection(source), actor=patient.account)
    services.request_generation(patient, key, job.pk, {'format':'json'}, dispatch=lambda _:None, actor=patient.account)
    if phase == 'download':
        services.generate_export(job.pk, store)
        job.refresh_from_db(); assert job.status == 'READY' and store.objects[job.object_key]
    seam = {'put':'put_staging', 'promote':'promote_immutable', 'download':'open_private'}[phase]
    original = getattr(store, seam)
    writers, pids = [], Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def concurrent_write(*args, **kwargs):
            result = original(*args, **kwargs)
            assert connection.in_atomic_block
            blocker = backend_pid()
            writers.append(pool.submit(thread_call,
                lambda: _decide(patient, source, 'CORRECT', changes={'url':SECOND_URL}), pids))
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
            return result
        monkeypatch.setattr(store, seam, concurrent_write)
        if phase == 'download':
            artifact = services.download_export(patient, key, job.pk, store, actor=patient.account)
        else:
            services.generate_export(job.pk, store)
        assert len(writers) == 1
        writers[0].result(timeout=20)
    assert not connection.in_atomic_block
    if phase == 'download':
        stream = GuardedStream(artifact.stream, lambda: services.validate_export_stream(job.pk, patient.account, key))
        assert stream.read(8) == b'' and stream.denied
        artifact.close()
    else:
        job.refresh_from_db(); assert job.byte_size > 100 and store.objects[job.object_key]
        with pytest.raises(ExportUnavailable):
            services.get_preview(patient, key, job.pk, actor=patient.account)
    job.refresh_from_db(); assert job.status == 'INVALIDATED' and job.snapshot == {}
    assert services.cleanup_export(job.pk, store)


@pytest.mark.parametrize('target', ['export','share'])
def test_mixed_output_waits_for_actual_partial_author_collector_commit(django_user_model, target):
    from apps.accounts.deletion import AccountDeletionOutcome, request_account_deletion, purge_account_deletion
    from apps.cloud_imaging.models import CloudImagingSource
    from tests.integration.test_glucose_export_postgres import authored_output_selection, existing_output
    owner, patient, actor, _, source, _ = setup(django_user_model, 'partial-' + target)
    chosen = authored_output_selection(patient, actor, 'mixed')
    chosen['cloud_source_ids'] = [str(source.pk)]
    output, check, expected_error = existing_output(owner, patient, chosen, target, django_user_model)
    job = request_account_deletion(actor.pk, document_dispatch=lambda _:None, account_dispatch=lambda _:None)
    entered, release = Event(), Event()
    writer_pids, reader_pids = Queue(), Queue()
    touched = []
    def pause_update(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith('UPDATE '):
            touched.append(sql.split()[1].strip('"'))
            if CloudImagingSource._meta.db_table == touched[-1] and not entered.is_set():
                entered.set(); assert release.wait(20)
        return result
    def purge_partial():
        with connection.execute_wrapper(pause_update):
            return purge_account_deletion(job.pk)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(thread_call, purge_partial, writer_pids)
        try:
            assert entered.wait(15)
            blocker = writer_pids.get(timeout=10)
            reader = pool.submit(thread_call, check, reader_pids)
            wait_until_backend_is_blocked_by(state, request_pid=reader_pids.get(timeout=10), blocker_pid=blocker)
        finally:
            release.set()
        assert writer.result(timeout=20).outcome == AccountDeletionOutcome.PURGED
        with pytest.raises(expected_error): reader.result(timeout=20)
    output.refresh_from_db(); assert output.snapshot == {}


def test_source_fk_only_commit_refetches_joined_author_after_wait(django_user_model):
    from apps.cloud_imaging.models import CloudImagingSource
    owner,patient,_,_,source,_ = setup(django_user_model,'source-fk-join')
    job=services.create_preview(patient,owner.session.session_key,source_selection(source),actor=patient.account)
    pids=Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            CloudImagingSource.objects.filter(pk=source.pk).update(created_by=None)
            blocker=backend_pid()
            reader=pool.submit(thread_call,lambda:services.get_preview(patient,owner.session.session_key,job.pk,actor=patient.account),pids)
            wait_until_backend_is_blocked_by(state,request_pid=pids.get(timeout=10),blocker_pid=blocker)
        with pytest.raises(ExportUnavailable):reader.result(timeout=20)
    job.refresh_from_db();assert job.snapshot=={}
