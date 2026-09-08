from copy import deepcopy
import io

import pytest
from pypdf import PdfReader

from apps.exports import services
from apps.exports.errors import ExportUnavailable, SnapshotChanged
from apps.exports.formats import build_artifact
from apps.exports.models import ExportJob
from apps.facts.readmodels import digest
from apps.patients.models import PatientShare
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_jobs import _preview, _ready
from tests.patients.test_family_shares import exchange
from .factories import stored_document
from .test_default_projection import ACCESS_URL, _fixture, _private_after


pytestmark = pytest.mark.django_db


def legacy_output(model, record):
    """Pre-policy snapshots legitimately permitted an arbitrary nickname.

    Keep its checksum coherent so the new access-string guard, rather than a
    corrupt snapshot digest, is what rejects consumption of stored old content.
    """
    record.refresh_from_db()
    old = deepcopy(record.snapshot)
    old['patient']['nickname'] = ACCESS_URL
    model.objects.filter(pk=record.pk).update(snapshot=old, snapshot_digest=digest(old))
    return old


@pytest.mark.parametrize('kind', ['original', 'zip'])
def test_old_original_or_original_only_zip_metadata_cannot_bypass_projection_policy(django_user_model, kind):
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, 'cloud-old-original-' + kind)
    document, _, store = stored_document(patient)
    snapshot = build_snapshot(patient, {'mode': 'all'})
    snapshot['documents'][0]['filename'] = ACCESS_URL
    with pytest.raises(SnapshotChanged):
        with build_artifact(snapshot, {'format': kind, 'parts': ['originals']}, store):
            pass


@pytest.mark.parametrize('route', ['html', 'pdf'])
def test_new_http_preview_omits_url_and_preserves_actual_private_sources(django_user_model, route):
    client, patient, document, version, private = _fixture(django_user_model)
    assert client.get('/records/').status_code == 200
    job = services.create_preview(patient, client.session.session_key, {'mode': 'all', 'details': True}, actor=patient.account)
    path = f'/visit/{job.pk}/' + ('pdf/' if route == 'pdf' else '')
    response = client.get(path)
    assert response.status_code == 200
    if route == 'pdf':
        payload = b''.join(response.streaming_content)
        response.close()
        text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages)
    else:
        text = response.content.decode()
    assert 'SYNTHETIC_SECRET' not in text and 'imaging.example.invalid' not in text
    assert '双肺结节' in text and '已省略外部访问内容' in text
    assert _private_after(document, version) == private


@pytest.mark.parametrize('phase', ['html', 'pdf', 'request', 'worker', 'publication', 'download'])
def test_old_stored_snapshot_is_scrubbed_at_each_actual_export_consumer(django_user_model, monkeypatch, phase):
    client, patient, _, _, job = _preview(django_user_model, 'cloud-old-' + phase)
    store = InMemoryObjectStore()
    if phase == 'download':
        _ready(patient, client, job, store)
        monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
    elif phase in {'worker', 'publication'}:
        services.request_generation(patient, client.session.session_key, job.pk, {'format': 'json'}, dispatch=lambda _: None)
    if phase == 'publication':
        real = services.build_artifact
        def build_then_change(*args, **kwargs):
            artifact = real(*args, **kwargs)
            legacy_output(ExportJob, job)
            return artifact
        monkeypatch.setattr(services, 'build_artifact', build_then_change)
    else:
        legacy_output(ExportJob, job)
    if phase in {'html', 'pdf', 'download'}:
        suffix = '' if phase == 'html' else phase + '/'
        response = client.get(f'/visit/{job.pk}/' + suffix)
        assert response.status_code == 409 and b'SYNTHETIC_SECRET' not in response.content
    elif phase == 'request':
        with pytest.raises(ExportUnavailable):
            services.request_generation(patient, client.session.session_key, job.pk, {'format': 'json'}, dispatch=lambda _: None)
    else:
        services.generate_export(job.pk, store)
        assert not store.objects
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending


@pytest.mark.parametrize('after_first_chunk', [False, True])
def test_download_rechecks_access_string_policy_at_real_stream_boundaries(django_user_model, monkeypatch, after_first_chunk):
    client, patient, _, _, job = _preview(django_user_model, 'cloud-stream-' + str(after_first_chunk))
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
    response = client.get(f'/visit/{job.pk}/download/')
    assert response.status_code == 200
    # Use small transport chunks to exercise both boundaries on a small fixture.
    # Production block size and its separate volume tests are unchanged.
    response.block_size = 32
    stream = iter(response.streaming_content)
    if after_first_chunk:
        assert next(stream)
    legacy_output(ExportJob, job)
    assert b''.join(stream) == b''
    response.close()
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}


@pytest.mark.parametrize('change_during_render', [False, True])
def test_shared_http_rejects_old_access_content_before_and_after_rendering(django_user_model, monkeypatch, change_during_render):
    from apps.patients import share_views
    from apps.patients.sharing import create_share

    _, patient, document, _, _ = _fixture(django_user_model)
    created = create_share(patient, patient.account, {'document_ids': [str(document.pk)], 'sections': ['patient', 'imaging']})
    reader, _ = _patient(django_user_model, 'cloud-old-share-' + str(change_during_render))
    share_id = exchange(reader, created.token)
    if change_during_render:
        real = share_views.render
        def changed(*args, **kwargs):
            response = real(*args, **kwargs)
            if args[1] == 'patients/shared_detail.html':
                legacy_output(PatientShare, created.share)
            return response
        monkeypatch.setattr(share_views, 'render', changed)
    else:
        legacy_output(PatientShare, created.share)
    response = reader.get(f'/shared/{share_id}/')
    assert response.status_code == 410 and b'SYNTHETIC_SECRET' not in response.content
    created.share.refresh_from_db()
    assert created.share.snapshot == {} and created.share.invalidated_at is not None
