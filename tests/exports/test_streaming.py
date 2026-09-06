import hashlib
import io
import zipfile

import pytest

from apps.exports.content import build_snapshot
from apps.exports.formats import build_artifact
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


class ChunkOnlyStore:
    def __init__(self, size):
        self.size = size
        self.maximum_request = 0

    def open_private(self, _key):
        owner = self
        class Source(io.RawIOBase):
            offset = 0
            def read(self, size=-1):
                owner.maximum_request = max(owner.maximum_request, size)
                assert 0 < size <= 128 * 1024, "Originals must be copied with bounded reads"
                length = min(size, owner.size - self.offset)
                self.offset += length
                return b"x" * length
        return Source()


def _case(django_user_model):
    _, patient = _patient(django_user_model, "streaming-original")
    document, _ = _document(patient)
    size = 8 * 1024 * 1024
    sha = hashlib.sha256()
    for _ in range(size // (64 * 1024)):
        sha.update(b"x" * (64 * 1024))
    type(document).objects.filter(pk=document.pk).update(byte_size=size, sha256=sha.hexdigest())
    return build_snapshot(patient, {"mode": "all"}), ChunkOnlyStore(size), sha.hexdigest()


@pytest.mark.parametrize("kind", ["original", "zip"])
def test_original_export_uses_bounded_reads_and_closes_private_temporary_file(django_user_model, kind):
    snapshot, store, expected = _case(django_user_model)
    with build_artifact(snapshot, {"format": kind, "parts": ["originals"]}, store) as artifact:
        assert store.maximum_request <= 128 * 1024
        if kind == "original":
            assert artifact.sha256 == expected and artifact.byte_size == store.size
        else:
            with zipfile.ZipFile(artifact.stream) as bundle:
                path = next(name for name in bundle.namelist() if name.startswith("originals/"))
                sha = hashlib.sha256()
                with bundle.open(path) as source:
                    for chunk in iter(lambda: source.read(64 * 1024), b""):
                        sha.update(chunk)
                assert sha.hexdigest() == expected
        stream = artifact.stream
    assert stream.closed


@pytest.mark.parametrize("kind", ["original", "zip"])
def test_corrupt_original_discards_partial_private_output(django_user_model, monkeypatch, kind):
    from apps.documents.errors import IntegrityMismatch
    from apps.exports import formats

    snapshot, store, _ = _case(django_user_model)
    store.size -= 1
    opened = []
    create = formats.private_temporary_file
    def temporary():
        output = create()
        opened.append(output)
        return output
    monkeypatch.setattr(formats, "private_temporary_file", temporary)
    with pytest.raises(IntegrityMismatch):
        build_artifact(snapshot, {"format": kind, "parts": ["originals"]}, store)
    assert opened and all(stream.closed for stream in opened)


@pytest.mark.parametrize("outcome", ["READY", "FAILED", "CANCELLED", "INVALIDATED"])
def test_generation_always_closes_built_artifact(django_user_model, monkeypatch, outcome):
    from apps.exports import services
    from apps.facts.revisions import revise_fact
    from tests.documents.fakes import InMemoryObjectStore
    from tests.exports.test_jobs import _preview

    client, patient, _, fact, job = _preview(django_user_model, "stream-cleanup-" + outcome)
    store = InMemoryObjectStore()
    store.fail_promote = outcome == "FAILED"
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    created = []
    build = services.build_artifact
    def interrupted_build(*args, **kwargs):
        artifact = build(*args, **kwargs)
        created.append(artifact.stream)
        if outcome == "CANCELLED":
            services.cancel_export(patient, client.session.session_key, job.pk)
        elif outcome == "INVALIDATED":
            revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
        return artifact
    monkeypatch.setattr(services, "build_artifact", interrupted_build)
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == outcome
    assert created and all(stream.closed for stream in created)


@pytest.mark.parametrize("failure", ["integrity", "temporary_space"])
def test_download_failure_is_visible_and_discards_private_output(django_user_model, monkeypatch, failure):
    from apps.exports import services
    from apps.exports.errors import ExportUnavailable
    from tests.documents.fakes import InMemoryObjectStore
    from tests.exports.test_jobs import _preview, _ready

    client, patient, _, _, job = _preview(django_user_model, "stream-download-" + failure)
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    opened = []
    create = services.private_temporary_file
    def temporary():
        if failure == "temporary_space":
            raise OSError("synthetic temporary storage failure")
        output = create()
        opened.append(output)
        return output
    monkeypatch.setattr(services, "private_temporary_file", temporary)
    if failure == "integrity":
        store.objects[job.object_key] = b"corrupted synthetic object"
    with pytest.raises(ExportUnavailable):
        services.download_export(patient, client.session.session_key, job.pk, store)
    assert all(stream.closed for stream in opened)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.cleanup_pending
    assert services.cleanup_export(job.pk, store)
    assert not store.objects
