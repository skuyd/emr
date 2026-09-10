"""Real COMMIT on a second backend fences selected molecular output."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import pytest
from django.db import connection, transaction

from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.integration.test_glucose_export_postgres import existing_output
from tests.exports.test_molecular_exports import ready_graph, selection
from tests.facts.pathology_factories import review

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql': pytest.skip('Requires isolated PostgreSQL')


@pytest.mark.parametrize('target', ['export', 'share'])
@pytest.mark.parametrize('change', ['identity', 'new_member'])
def test_waiting_output_rejects_real_committed_dependency_change(django_user_model, target, change):
    from tests.facts.molecular_factories import add
    client, patient, document, report, fields = ready_graph(django_user_model, 'molecular-pg-' + target + change)
    output, check, expected = existing_output(client, patient, selection(document, fields['metric']), target, django_user_model)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'identity': review(patient, fields['identity'], 'EXCLUDE')
            else: add(patient, report, 'assay.name', 'assay:a', {'text': '新增检测名称'}, {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']})
            blocker = backend_pid(); future = pool.submit(thread_call, check, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(expected): future.result(timeout=20)
    output.refresh_from_db()
    assert output.snapshot == {}


@pytest.mark.parametrize('target', ['export', 'share'])
def test_real_author_collector_commit_after_render_discards_old_body(django_user_model, monkeypatch, target):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.patients.models import PatientMembership
    from apps.exports.services import create_preview
    from apps.patients.sharing import create_share
    from tests.documents.test_detail_viewer import _patient
    from tests.patients.test_family_shares import exchange
    from apps.exports import views
    from apps.patients import share_views
    client, patient, document, _, fields = ready_graph(django_user_model, 'molecular-author-pg-' + target)
    _, own = _patient(django_user_model, 'molecular-retired-author-' + target)
    PatientMembership.objects.create(patient=patient, account=own.account, role='EDITOR')
    review(patient, fields['identity'], actor=own.account)
    review(patient, fields['metric'])
    chosen = selection(document, fields['metric'])
    if target == 'export':
        client.get('/records/'); output = create_preview(patient, client.session.session_key, chosen, actor=patient.account)
        url, expected = f'/visit/{output.pk}/', 409
        path, render = 'apps.exports.views._render', views._render
    else:
        created = create_share(patient, patient.account, chosen); output = created.share
        client, _ = _patient(django_user_model, 'molecular-retired-reader')
        url, expected = f'/shared/{exchange(client, created.token)}/', 410
        path, render = 'apps.patients.share_views.render', share_views.render
    job = request_account_deletion(own.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert client.get(url).status_code == 200
    changed, pids = [], Queue(); reader = backend_pid()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def during(*args, **kwargs):
            response = render(*args, **kwargs)
            if not changed:
                assert 'NM_SYN.2' in response.content.decode()
                changed.append(True)
                result = pool.submit(thread_call, lambda: purge_account_deletion(job.pk), pids).result(timeout=30)
                assert pids.get(timeout=5) != reader and result.outcome == 'PURGED'
            return response
        monkeypatch.setattr(path, during)
        response = client.get(url)
    assert changed == [True] and response.status_code == expected
    assert 'NM_SYN.2' not in response.content.decode()
    output.refresh_from_db(); assert output.snapshot == {}


@pytest.mark.parametrize('phase', ['publish', 'download', 'first_chunk', 'next_chunk'])
def test_collector_commit_during_storage_or_before_each_chunk_blocks_molecular_bytes(django_user_model, monkeypatch, phase):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.patients.models import PatientMembership
    from apps.exports import services
    from apps.exports.errors import ExportUnavailable
    from tests.documents.test_detail_viewer import _patient
    from tests.documents.fakes import InMemoryObjectStore
    client, patient, document, _, fields = ready_graph(django_user_model, 'molecular-io-' + phase)
    _, own = _patient(django_user_model, 'molecular-io-author-' + phase)
    PatientMembership.objects.create(patient=patient, account=own.account, role='EDITOR')
    review(patient, fields['identity'], actor=own.account); review(patient, fields['metric'])
    client.get('/visit/'); key = client.session.session_key
    output = services.create_preview(patient, key, selection(document, fields['metric']), actor=patient.account)
    services.request_generation(patient, key, output.pk, {'format': 'json'}, dispatch=lambda _: None, actor=patient.account)
    store = InMemoryObjectStore()
    if phase != 'publish':
        services.generate_export(output.pk, store)
        output.refresh_from_db(); assert output.status == 'READY' and output.byte_size > 100
    deletion = request_account_deletion(own.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    pids, committed = Queue(), []
    reader = backend_pid()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def commit():
            result = pool.submit(thread_call, lambda: purge_account_deletion(deletion.pk), pids).result(timeout=30)
            assert pids.get(timeout=5) != reader and result.outcome == 'PURGED'
            committed.append(True)
        if phase == 'publish':
            promote = store.promote_immutable
            def during(*args, **kwargs):
                result = promote(*args, **kwargs); commit(); return result
            monkeypatch.setattr(store, 'promote_immutable', during)
            services.generate_export(output.pk, store)
        elif phase == 'download':
            original = store.open_private
            def during(*args, **kwargs):
                stream = original(*args, **kwargs); commit(); return stream
            monkeypatch.setattr(store, 'open_private', during)
            with pytest.raises(ExportUnavailable): services.download_export(patient, key, output.pk, store, actor=patient.account)
        else:
            monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
            response = client.get(f'/visit/{output.pk}/download/')
            assert response.status_code == 200
            stream = response._guarded_stream
            try:
                if phase == 'next_chunk': assert stream.read(32)
                commit()
                assert stream.read(32) == b'' and stream.denied
            finally: response.close()
    assert committed == [True]
    output.refresh_from_db(); assert output.status == 'INVALIDATED' and output.snapshot == {}
