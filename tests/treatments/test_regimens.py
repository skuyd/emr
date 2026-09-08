import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.patients.models import PatientMembership
from apps.treatments.readmodels import event_token, treatment_material
from tests.documents.test_detail_viewer import _patient
from tests.treatments.test_manual_events import create

pytestmark = pytest.mark.django_db


def regimen(patient, event, **kwargs):
    from apps.treatments.regimens import create_regimen
    current = next(e for e in treatment_material(patient, actor=patient.account)["events"] if e["id"] == str(event.pk))
    values = dict(actor=patient.account, text="本人记录的方案甲", event_ids=[event.pk],
                  expected_sources={str(event.pk): event_token(current)}, checked_original=True, operation_id=uuid.uuid4())
    values.update(kwargs)
    return create_regimen(patient, **values)


def test_regimen_keeps_source_and_true_actor_without_inventing_start_end(django_user_model):
    _, patient = _patient(django_user_model, "regimen-create")
    event = create(patient, patient.account)
    row = regimen(patient, event)
    assert row.created_by_id == patient.account_id and row.revision_number == 1
    assert row.current_content["actual_start"] is None and row.current_content["actual_end"] is None
    assert set(row.events.values_list("pk", flat=True)) == {event.pk}
    assert treatment_material(patient, actor=patient.account)["regimens"][0]["usable"]


def test_regimen_correction_preserves_original_and_invalidates_cycle_dependency(django_user_model):
    from apps.treatments.regimens import revise_regimen
    from tests.treatments.test_cycle_decisions import cycle
    _, patient = _patient(django_user_model, "regimen-correct")
    event = create(patient, patient.account)
    row = regimen(patient, event)
    item = cycle(patient, [event], regimen_id=row.pk)
    tokens = row.current_content["event_tokens"]
    revise_regimen(patient, row.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                    changes={"text": "本人更正的方案乙"}, checked_original=True, expected_sources=tokens, operation_id=uuid.uuid4())
    row.refresh_from_db()
    assert row.initial_content["text"] == "本人记录的方案甲"
    assert row.current_content["text"] == "本人更正的方案乙"
    assert row.revisions.count() == 2
    current = treatment_material(patient, actor=patient.account)
    assert next(c for c in current["cycles"] if c["id"] == str(item.pk))["status"] == "STALE"


def test_regimen_unknown_name_and_foreign_sources_are_rejected(django_user_model):
    _, patient = _patient(django_user_model, "regimen-invalid")
    _, other = _patient(django_user_model, "regimen-other")
    event = create(patient, patient.account)
    with pytest.raises(ValidationError):
        regimen(patient, event, text=" ")
    with pytest.raises(PermissionDenied):
        regimen(patient, event, event_ids=[create(other, other.account).pk])


def test_same_text_can_have_distinct_explicit_regimen_episodes_but_operation_replays_once(django_user_model):
    _, patient = _patient(django_user_model, "regimen-episode")
    event = create(patient, patient.account)
    operation = uuid.uuid4()
    first = regimen(patient, event, operation_id=operation)
    same = regimen(patient, event, operation_id=operation)
    another = regimen(patient, event)
    assert first.pk == same.pk and another.pk != first.pk
    assert first.normalized_key == another.normalized_key and first.episode_key != another.episode_key


def test_read_only_members_cannot_create_regimen(django_user_model):
    _, patient = _patient(django_user_model, "regimen-owner")
    _, member = _patient(django_user_model, "regimen-viewer")
    PatientMembership.objects.create(patient=patient, account=member.account, role="VIEWER")
    event = create(patient, patient.account)
    with pytest.raises(PermissionDenied):
        regimen(patient, event, actor=member.account)


def test_stale_regimen_cannot_be_reused_or_silently_reconfirmed_on_cycle(django_user_model):
    from apps.treatments.cycles import revise_cycle
    from apps.treatments.regimens import revise_regimen
    from apps.treatments.services import TreatmentConflict, revise_event
    from tests.treatments.test_cycle_decisions import cycle
    _, patient = _patient(django_user_model, "regimen-stale-cycle")
    event = create(patient, patient.account)
    first = regimen(patient, event)
    item = cycle(patient, [event], regimen_id=first.pk)
    revise_regimen(patient, first.pk, actor=patient.account, action="CORRECT", expected_revision=1,
        changes={"text": "更正后方案"}, expected_sources=first.current_content["event_tokens"], checked_original=True,
        operation_id=uuid.uuid4())
    with pytest.raises(TreatmentConflict):
        revise_cycle(patient, item.pk, actor=patient.account, action="CONFIRM", expected_revision=1,
                     checked_original=True, operation_id=uuid.uuid4())
    revise_event(patient, event.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                 changes={"title": "更正后记录"}, checked_original=True, operation_id=uuid.uuid4())
    with pytest.raises(TreatmentConflict):
        cycle(patient, [event], regimen_id=first.pk)


def test_explicit_regimen_reassignment_updates_dependency_and_history(django_user_model):
    from apps.treatments.cycles import revise_cycle
    from tests.treatments.test_cycle_decisions import cycle
    _, patient = _patient(django_user_model, "regimen-reassignment")
    event = create(patient, patient.account)
    first, second = regimen(patient, event), regimen(patient, event, text="本人记录的另一方案")
    item = cycle(patient, [event], regimen_id=first.pk)
    revision = revise_cycle(patient, item.pk, actor=patient.account, action="CORRECT", expected_revision=1,
        changes={"regimen_id": str(second.pk)}, checked_original=True, operation_id=uuid.uuid4())
    assert revision.before["regimen_id"] == str(first.pk) and revision.after["regimen_id"] == str(second.pk)
    assert next(c for c in treatment_material(patient, actor=patient.account)["cycles"] if c["id"] == str(item.pk))["usable"]
