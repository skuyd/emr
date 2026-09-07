"""Measure real FileResponse iteration, including its final audit insert."""

import hashlib
import json
import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.exports.services import create_preview, generate_export, request_generation
from apps.patients.sharing import create_share
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient
from tests.patients.test_family_shares import exchange

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind,document_count", [("original", 1), ("share", 1), ("share", 25), ("export", 1), ("export", 25)])
def test_one_megabyte_download_revalidates_with_bounded_chunks(django_user_model, monkeypatch, kind, document_count):
    owner, patient = _patient(django_user_model, f"stream-volume-{kind}-{document_count}")
    documents = [_document(patient)[0] for _ in range(document_count)]
    payload = b"x" * 1024 * 1024
    store = InMemoryObjectStore()
    store.objects[documents[0].original_object_key] = payload
    owner.get("/records/")
    if kind == "original":
        monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
        response = owner.get(f"/records/{documents[0].pk}/original/")
    elif kind == "share":
        reader, _ = _patient(django_user_model, f"stream-volume-reader-{document_count}")
        created = create_share(patient, patient.account, {"document_ids": [str(row.pk) for row in documents], "sections": ["sources"]}, allow_original_download=True)
        share_id = exchange(reader, created.token)
        monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
        response = reader.get(f"/shared/{share_id}/documents/{documents[0].pk}/original/")
    else:
        job = create_preview(patient, owner.session.session_key, {"mode": "all"}, actor=patient.account)
        request_generation(patient, owner.session.session_key, job.pk, {"format": "json"}, actor=patient.account, dispatch=lambda _: None)
        generate_export(job.pk, store)
        job.refresh_from_db()
        assert job.status == "READY"
        # Keep the complete real generation/permission path, then substitute a
        # verified 1 MiB synthetic artifact solely for comparable flow volume.
        store.objects[job.object_key] = payload
        job.byte_size, job.sha256 = len(payload), hashlib.sha256(payload).hexdigest()
        job.save(update_fields=["byte_size", "sha256"])
        monkeypatch.setattr("apps.exports.views.get_object_store", lambda: store)
        response = owner.get(f"/visit/{job.pk}/download/")
    assert response.status_code == 200 and response.file_to_stream is None
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        chunks = list(response.streaming_content)
    elapsed = time.perf_counter() - started
    assert b"".join(chunks) == payload
    assert len(chunks) <= 8 and len(queries) < 2000
    print(json.dumps({"kind": kind, "documents": document_count, "bytes": len(payload), "chunks": len(chunks),
                      "queries": len(queries), "seconds": round(elapsed, 4), "database": connection.vendor,
                      "measurement": "streaming_content_only_includes_final_audit"}))
    response.close()
