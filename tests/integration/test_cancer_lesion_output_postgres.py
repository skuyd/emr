"""Committed changes in each selected domain stop the actual combined stream."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.db import connection

from apps.exports import services, views
from apps.lesions.services import rename_lesion
from apps.operations.models import AuditEvent
from tests.cancer_ordering.test_lesion_output_compatibility import mixed_selected
from tests.cancer_ordering.test_services import _select
from tests.cloud_imaging.test_source_services import _decide
from tests.documents.fakes import InMemoryObjectStore
from tests.exports.test_lesion_output_bindings import authenticated
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize('domain', ['cancer', 'lesion', 'cloud'])
def test_combined_stream_stops_after_other_connection_commits(django_user_model, monkeypatch, record_property, domain):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires an isolated PostgreSQL database')
    patient, _, lesion, cloud, scope = mixed_selected(django_user_model)
    client = authenticated(patient)
    job = services.create_preview(patient, client.session.session_key, scope, actor=patient.account)
    assert all(job.snapshot[key] for key in ('cancer_candidates', 'lesions', 'cloud_imaging_sources'))
    services.request_generation(patient, client.session.session_key, job.pk, {'format': 'json'},
                                dispatch=lambda *_: None, actor=patient.account)
    store = InMemoryObjectStore()
    services.generate_export(job.pk, store)
    monkeypatch.setattr(views, 'get_object_store', lambda: store)
    response = client.get(f'/visit/{job.pk}/download/', {'patient': str(patient.pk)})
    assert response.status_code == 200
    response.block_size = 128
    stream = iter(response.streaming_content)
    assert len(next(stream)) == 128
    def change():
        assert not connection.in_atomic_block
        if domain == 'cancer':
            _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
        elif domain == 'lesion':
            rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                          expected_revision=lesion.revision_number, name='SYNTHETIC_CHANGED_LESION')
        else:
            _decide(patient, cloud, 'EXCLUDE')
        assert not connection.in_atomic_block
    pids, reader_pid = Queue(), backend_pid()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(thread_call, change, pids)
            writer_pid = pids.get(timeout=10)
            assert writer_pid != reader_pid
            future.result(timeout=30)
        record_property('reader_backend_pid', reader_pid)
        record_property('writer_backend_pid', writer_pid)
        assert b''.join(stream) == b''
    finally:
        response.close()
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}
    assert not job.cloud_sources.exists()
    assert AuditEvent.objects.filter(action='export_downloaded', result='denied').count() == 1
