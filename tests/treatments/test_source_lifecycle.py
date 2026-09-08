"""Changed, withdrawn or purged sources cannot leave usable treatment payloads."""

import uuid

import pytest
from django.core.exceptions import ValidationError

from apps.documents.deletion import purge_document_deletion, request_document_deletion
from apps.documents.lifecycle import move_to_trash, restore_document
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.treatments.models import TreatmentEvent, TreatmentRevision
from apps.treatments.readmodels import treatment_material
from apps.treatments.services import TreatmentConflict, revise_event
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.treatments.factories import source_event
from tests.treatments.test_manual_events import create


pytestmark = pytest.mark.django_db


def confirmed(patient):
    event, fact, document = source_event(patient)
    row = treatment_material(patient, actor=patient.account)["events"][0]
    tokens = {s["id"]: s["current_source_token"] for s in row["sources"]}
    revise_event(patient, event.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                 expected_sources=tokens, checked_original=True, operation_id=uuid.uuid4())
    return event, fact, document


def test_confirming_a_treatment_does_not_confirm_the_original_fact(django_user_model):
    _, patient = _patient(django_user_model, "treatment-source-confirm")
    event, fact, _ = confirmed(patient)
    assert treatment_material(patient, actor=patient.account)["events"][0]["usable"] is True
    fact.refresh_from_db()
    assert fact.revision_number == 0 and effective_fact(fact)["status"] == "PENDING"


@pytest.mark.parametrize("change", ["fact", "trash_restore", "material"])
def test_source_changes_invalidate_confirmation_and_cannot_be_refreshed_by_replay(django_user_model, change):
    _, patient = _patient(django_user_model, "treatment-source-change")
    event, fact, document = confirmed(patient)
    if change == "fact":
        revise_fact(patient, fact.pk, actor=patient.account, action="CORRECT", expected_revision=0,
                    changes={"text": "2024-03-02 合成来源已更正。"}, checked_original=True)
    elif change == "trash_restore":
        move_to_trash(patient, document.pk, actor=patient.account)
        restore_document(patient, document.pk, actor=patient.account)
    else:
        document.material_revision += 1
        document.save(update_fields=["material_revision", "updated_at"])
    row = treatment_material(patient, actor=patient.account)["events"][0]
    assert row["status"] == "STALE" and not row["usable"]
    with pytest.raises(TreatmentConflict):
        revise_event(patient, event.pk, actor=patient.account, action="CONFIRM", expected_revision=1,
                     expected_sources={s["id"]: s["current_source_token"] for s in row["sources"]},
                     checked_original=True, operation_id=uuid.uuid4())


def test_permanent_document_purge_removes_dependent_treatment_and_medical_history(django_user_model):
    _, patient = _patient(django_user_model, "treatment-source-purge")
    event, fact, document = confirmed(patient)
    independent = create(patient, patient.account)
    assert TreatmentRevision.objects.filter(event=event).exists()
    job = request_document_deletion(patient, document.pk, actor=patient.account, dispatch=lambda *_: None)
    result = purge_document_deletion(job.pk, InMemoryObjectStore())
    assert result.outcome == "PURGED"
    assert not TreatmentEvent.objects.filter(pk=event.pk).exists()
    assert not TreatmentRevision.objects.filter(event_id=event.pk).exists()
    assert TreatmentEvent.objects.filter(pk=independent.pk).exists()
    assert [row["id"] for row in treatment_material(patient, actor=patient.account)["events"]] == [str(independent.pk)]


def test_raw_baseline_and_appended_revision_cannot_be_overwritten(django_user_model):
    _, patient = _patient(django_user_model, "treatment-source-immutable")
    event, _, _ = confirmed(patient)
    event.initial_content["title"] = "覆盖原始内容"
    with pytest.raises(ValidationError):
        event.save(update_fields=["initial_content"])
    revision = TreatmentRevision.objects.get(event=event)
    revision.after["title"] = "覆盖历史"
    with pytest.raises(ValidationError):
        revision.save()
