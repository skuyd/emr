"""Omitted access text still binds rendered and streamed output to full sources."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Event

import pytest
from django.db import connection, transaction

from apps.cloud_imaging.projection import OMITTED, project_default_snapshot
from apps.facts.readmodels import effective_fact
from apps.patients.sharing import create_share
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_lesion_cloud_projection import ACCESS_URL, projected_lesion
from tests.exports.test_lesion_output_bindings import authenticated
from tests.facts.test_scoped_laterality import confirm
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.patients.test_family_shares import exchange


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated lesion/cloud integration PostgreSQL database')


def change_hidden_access(patient, report):
    field = report.fields.get(field_key='lesion.dimensions')
    before = deepcopy(effective_fact(field)['content']['value'])
    after = deepcopy(before)
    after['raw'] = after['raw'].replace('LESION_SYNTHETIC_SECRET', 'NEW_SYNTHETIC_SECRET')
    assert before != after
    assert project_default_snapshot(before) == project_default_snapshot(after)
    with transaction.atomic():
        confirm(patient, field, action='CORRECT', changes={'value': after, 'raw_value': after['raw']})
    assert not connection.in_atomic_block
    field.refresh_from_db()
    assert effective_fact(field)['content']['value']['raw'] == after['raw']


def test_omitted_url_revision_stops_actual_json_stream_after_normal_commit(django_user_model, monkeypatch, record_property):
    from apps.exports import services, views
    from apps.operations.models import AuditEvent

    patient, _, report, scope = projected_lesion(django_user_model, 'dimensions', 'pg-stream')
    client = authenticated(patient)
    job = services.create_preview(patient, client.session.session_key, scope, actor=patient.account)
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
    pids, reader_pid = Queue(), backend_pid()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: change_hidden_access(patient, report), pids)
        writer_pid = pids.get(timeout=10)
        assert writer_pid != reader_pid
        future.result(timeout=20)
    record_property('reader_backend_pid', reader_pid)
    record_property('writer_backend_pid', writer_pid)
    try:
        assert b''.join(stream) == b''
    finally:
        response.close()
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}
    assert AuditEvent.objects.filter(action='export_downloaded', result='denied').count() == 1


def test_rendered_omission_is_discarded_after_normal_source_commit(django_user_model, monkeypatch, record_property):
    from apps.patients import share_views

    patient, _, report, scope = projected_lesion(django_user_model, 'dimensions', 'pg-render')
    made = create_share(patient, patient.account, scope)
    reader, _ = _patient(django_user_model, 'lesion-cloud-pg-share-reader')
    identity = exchange(reader, made.token)
    entered, release, pids = Event(), Event(), Queue()
    original_render = share_views.render

    def pause_after_render(request, template, *args, **kwargs):
        response = original_render(request, template, *args, **kwargs)
        if template == 'patients/shared_detail.html':
            assert OMITTED.encode() in response.content
            assert ACCESS_URL.encode() not in response.content
            entered.set()
            assert release.wait(timeout=20)
        return response

    monkeypatch.setattr(share_views, 'render', pause_after_render)
    writer_pid = backend_pid()
    assert not connection.in_atomic_block
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: reader.get(f'/shared/{identity}/'), pids)
        try:
            reader_pid = pids.get(timeout=10)
            assert reader_pid != writer_pid
            assert entered.wait(timeout=15)
            change_hidden_access(patient, report)
        finally:
            release.set()
        response = future.result(timeout=20)
    record_property('reader_backend_pid', reader_pid)
    record_property('writer_backend_pid', writer_pid)
    assert response.status_code == 410 and OMITTED.encode() not in response.content
    made.share.refresh_from_db()
    assert made.share.invalidated_at is not None and made.share.snapshot == {}
