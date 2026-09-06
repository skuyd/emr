from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

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
