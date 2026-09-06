from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.documents.deletion import purge_document_deletion, DeletionOutcome
from apps.documents.lifecycle import LifecycleUnavailable, expire_trash, move_to_trash, permanently_delete_from_trash, restore_document
from apps.documents.models import Document, DocumentDeletionJob
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def _thread(barrier, action):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        try:
            action()
            return "done"
        except LifecycleUnavailable:
            return "unavailable"
    finally:
        close_old_connections()


def test_restore_and_permanent_delete_have_one_winner_and_never_delete_a_restored_original(django_user_model):
    _, patient = _patient(django_user_model, "pg-trash")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    move_to_trash(patient, document.pk)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        restored = pool.submit(_thread, barrier, lambda: restore_document(patient, document.pk))
        deleted = pool.submit(_thread, barrier, lambda: permanently_delete_from_trash(patient, document.pk, dispatch=lambda job: None))
        outcomes = (restored.result(timeout=15), deleted.result(timeout=15))
    assert sorted(outcomes) == ["done", "unavailable"]
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic-original"
    job = DocumentDeletionJob.objects.filter(document=document).first()
    if outcomes[0] == "done":
        assert job is None
        assert Document.objects.get(pk=document.pk).deleted_at is None
        assert store.objects[document.original_object_key] == b"synthetic-original"
    else:
        assert purge_document_deletion(job.pk, store).outcome == DeletionOutcome.PURGED
        assert not Document.objects.filter(pk=document.pk).exists()


def test_expiry_cannot_purge_when_restore_wins_the_shared_state_transition(django_user_model):
    _, patient = _patient(django_user_model, "pg-expiry")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    now = timezone.now()
    move_to_trash(patient, document.pk, now=now)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        restored = pool.submit(_thread, barrier, lambda: restore_document(patient, document.pk, now=now + timedelta(days=30, microseconds=-1)))
        expired = pool.submit(_thread, barrier, lambda: expire_trash(dispatch=lambda job: None, now=now + timedelta(days=30)))
        restored_result = restored.result(timeout=15)
        assert expired.result(timeout=15) == "done"
    job = DocumentDeletionJob.objects.filter(document=document).first()
    if restored_result == "done":
        assert job is None and Document.objects.get(pk=document.pk).deleted_at is None
    else:
        assert job is not None


def _run_export_thread(action):
    close_old_connections()
    try:
        return action()
    finally:
        close_old_connections()


def test_source_trash_during_export_build_fences_publication_without_orphans(django_user_model, monkeypatch):
    from apps.exports import services
    from tests.exports.test_jobs import _preview

    client, patient, document, _, job = _preview(django_user_model, "pg-export-source")
    store = InMemoryObjectStore()
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    entered, release = Event(), Event()
    build = services.build_artifact
    def paused_build(*args, **kwargs):
        artifact = build(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=15)
        return artifact
    monkeypatch.setattr(services, "build_artifact", paused_build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        generation = pool.submit(_run_export_thread, lambda: services.generate_export(job.pk, store))
        assert entered.wait(timeout=10)
        try:
            move_to_trash(patient, document.pk)
        finally:
            release.set()
        generation.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "INVALIDATED"
    assert services.cleanup_export(job.pk, store)
    assert not store.objects


def test_cancel_waits_for_storage_commit_then_cleans_the_published_keys(django_user_model):
    from apps.exports import services
    from tests.exports.test_jobs import _preview

    client, patient, _, _, job = _preview(django_user_model, "pg-export-cancel")
    entered, release = Event(), Event()
    class PausedStore(InMemoryObjectStore):
        def promote_immutable(self, *args, **kwargs):
            result = super().promote_immutable(*args, **kwargs)
            entered.set()
            assert release.wait(timeout=15)
            return result
    store = PausedStore()
    key = client.session.session_key
    services.request_generation(patient, key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        generation = pool.submit(_run_export_thread, lambda: services.generate_export(job.pk, store))
        assert entered.wait(timeout=10)
        cancel = pool.submit(_run_export_thread, lambda: services.cancel_export(patient, key, job.pk))
        release.set()
        generation.result(timeout=15)
        cancel.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "CANCELLED"
    assert services.cleanup_export(job.pk, store)
    assert not store.objects
