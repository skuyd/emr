"""Actual committed lifecycle changes between rendering and response return."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection
from django.urls import reverse

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.accounts.models import AccountDeletionJob
from apps.documents.lifecycle import move_to_trash
from apps.treatments import views
from tests.documents.test_detail_viewer import _patient
from tests.integration.test_family_postgres_concurrency import thread_call
from tests.patients.test_family_access import family
from tests.treatments.factories import source_event
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create
from tests.treatments.test_regimens import regimen

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def _render_then_commit(monkeypatch, request, mutate):
    entered, resume = Event(), Event()
    render = views.render

    def paused(*args, **kwargs):
        response = render(*args, **kwargs)
        entered.set()
        assert resume.wait(timeout=20)
        return response

    monkeypatch.setattr(views, "render", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, request)
        try:
            assert entered.wait(timeout=20)
            mutate()
        finally:
            resume.set()
        return future.result(timeout=20)


@pytest.mark.parametrize("route", ["event", "cycle_new", "assign"])
def test_source_removal_committed_after_real_html_render_is_not_returned(django_user_model, monkeypatch, route):
    client, patient = _patient(django_user_model, "pg-treatment-read-source-" + route)
    event, _, document = source_event(patient)
    item = cycle(patient, [event]) if route == "assign" else None
    args = [event.pk] if route == "event" else [item.pk] if item else []
    response = _render_then_commit(monkeypatch,
        lambda: client.get(reverse("treatments:" + route, args=args)),
        lambda: move_to_trash(patient, document.pk, actor=patient.account))
    document.refresh_from_db()
    assert document.deleted_at is not None
    assert response.status_code == 409 and str(event.pk) not in response.content.decode()


@pytest.mark.parametrize("kind", ["event", "regimen", "cycle"])
def test_real_account_purge_during_render_cannot_return_historical_author(django_user_model, monkeypatch, kind):
    owner, patient, _, actor, _ = family(django_user_model, "pg-treatment-read-author-" + kind)
    event = create(patient, actor)
    record = event if kind == "event" else regimen(patient, event, actor=actor) if kind == "regimen" else cycle(patient, [event], actor=actor)
    identity = str(actor.pk)

    def purge():
        request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        job = AccountDeletionJob.objects.get(account_id=actor.pk)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED

    response = _render_then_commit(monkeypatch,
        lambda: owner.get(reverse("treatments:" + kind, args=[record.pk])), purge)
    assert not record.revisions.filter(author_id=identity).exists()
    assert response.status_code == 409 and identity not in response.content.decode()
