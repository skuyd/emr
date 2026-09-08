"""A rendered treatment page must retain its source and history identity."""
import pytest
from django.urls import reverse

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.accounts.models import AccountDeletionJob
from apps.documents.lifecycle import move_to_trash
from apps.treatments import views
from apps.treatments.readmodels import effective_event
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_access import family
from tests.treatments.factories import source_event
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create
from tests.treatments.test_regimens import regimen

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("route", ["index", "event", "regimen", "cycle", "regimen_new", "cycle_new", "merge", "split", "assign"])
def test_rendered_source_and_choice_pages_reject_a_committed_lifecycle_change(django_user_model, monkeypatch, route):
    client, patient = _patient(django_user_model, "treatment-fresh-source-" + route)
    event, _, document = source_event(patient)
    scheme = regimen(patient, event)
    item = cycle(patient, [event], regimen_id=scheme.pk)
    identities = {"event": event.pk, "regimen": scheme.pk, "cycle": item.pk, "split": item.pk, "assign": item.pk}
    path = reverse("treatments:" + route, args=[identities[route]] if route in identities else [])
    assert client.get(path).status_code == 200
    render = views.render

    def delete_after_render(*args, **kwargs):
        response = render(*args, **kwargs)
        assert response.status_code == 200
        move_to_trash(patient, document.pk, actor=patient.account)
        return response

    monkeypatch.setattr(views, "render", delete_after_render)
    response = client.get(path)
    assert not effective_event(event)["source_valid"]
    assert response.status_code == 409
    assert "请刷新" in response.content.decode()
    assert str(event.pk) not in response.content.decode()
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.parametrize("kind", ["event", "regimen", "cycle"])
def test_rendered_history_never_returns_an_author_purged_before_the_response(django_user_model, monkeypatch, kind):
    owner, patient, _, actor, _ = family(django_user_model, "treatment-fresh-author-" + kind)
    event = create(patient, actor)
    record = event if kind == "event" else regimen(patient, event, actor=actor) if kind == "regimen" else cycle(patient, [event], actor=actor)
    identity = str(actor.pk)
    render = views.render

    def purge_after_render(*args, **kwargs):
        response = render(*args, **kwargs)
        assert identity in response.content.decode()
        request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        job = AccountDeletionJob.objects.get(account_id=actor.pk)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
        return response

    monkeypatch.setattr(views, "render", purge_after_render)
    response = owner.get(reverse("treatments:" + kind, args=[record.pk]))
    assert not record.revisions.filter(author_id=identity).exists()
    assert response.status_code == 409
    assert identity not in response.content.decode()


def test_invalid_post_does_not_redisplay_a_source_changed_during_render(django_user_model, monkeypatch):
    client, patient = _patient(django_user_model, "treatment-fresh-invalid-post")
    event, _, document = source_event(patient)
    render = views.render

    def delete_after_render(*args, **kwargs):
        response = render(*args, **kwargs)
        assert response.status_code == 400
        move_to_trash(patient, document.pk, actor=patient.account)
        return response

    monkeypatch.setattr(views, "render", delete_after_render)
    response = client.post(reverse("treatments:event", args=[event.pk]), {"patient_id": str(patient.pk), "action": "CORRECT"})
    assert response.status_code == 409
    assert str(event.pk) not in response.content.decode()
