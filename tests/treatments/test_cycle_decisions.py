"""Patient decisions preserve every baseline, conflict and merge/split parent."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.patients.models import PatientMembership
from apps.treatments.readmodels import event_token, treatment_material
from tests.documents.test_detail_viewer import _patient
from tests.treatments.test_manual_events import create


pytestmark = pytest.mark.django_db


def cycle(patient, events, **kwargs):
    from apps.treatments.cycles import create_cycle

    rows = {r["id"]: r for r in treatment_material(patient, actor=patient.account)["events"]}
    values = dict(actor=patient.account, event_ids=[e.pk for e in events],
                  expected_sources={str(e.pk): event_token(rows[str(e.pk)]) for e in events},
                  anchor="2024-02-29", anchor_precision="DAY", ordinal=None, regimen_id=None,
                  checked_original=True, operation_id=uuid.uuid4())
    values.update(kwargs)
    return create_cycle(patient, **values)


def test_manual_cycle_keeps_unknown_ordinal_and_does_not_invent_end(django_user_model):
    _, patient = _patient(django_user_model, "cycle-manual-unknown")
    event = create(patient, patient.account)
    item = cycle(patient, [event])
    assert item.current_content["ordinal"] is None
    assert item.current_content["end"] is None
    assert item.current_content["anchor"] == "2024-02-29"
    assert item.origin == "USER"
    material = treatment_material(patient, actor=patient.account)
    assert material["cycles"][0]["usable"] is True


def test_cycle_creation_rejects_foreign_events_and_stale_evidence(django_user_model):
    from apps.treatments.cycles import create_cycle
    from apps.treatments.services import TreatmentConflict, revise_event

    _, patient = _patient(django_user_model, "cycle-create-owner")
    _, other = _patient(django_user_model, "cycle-create-foreign")
    event = create(patient, patient.account)
    foreign = create(other, other.account)
    with pytest.raises(PermissionDenied):
        cycle(patient, [event], event_ids=[foreign.pk])
    rows = treatment_material(patient, actor=patient.account)["events"]
    stale = {str(event.pk): event_token(rows[0])}
    revise_event(patient, event.pk, actor=patient.account, expected_revision=1, action="REVOKE", operation_id=uuid.uuid4())
    with pytest.raises(TreatmentConflict):
        create_cycle(patient, actor=patient.account, event_ids=[event.pk], expected_sources=stale,
                     anchor="2024-02-29", anchor_precision="DAY", ordinal=None, regimen_id=None,
                     checked_original=True, operation_id=uuid.uuid4())


def test_event_correction_invalidates_dependent_confirmed_cycle(django_user_model):
    from apps.treatments.services import revise_event

    _, patient = _patient(django_user_model, "cycle-event-change")
    event = create(patient, patient.account)
    item = cycle(patient, [event])
    revise_event(patient, event.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                 changes={"date": "2024-03-02"}, checked_original=True, operation_id=uuid.uuid4())
    material = treatment_material(patient, actor=patient.account)
    row = next(r for r in material["cycles"] if r["id"] == str(item.pk))
    assert row["status"] == "STALE" and not row["usable"]


def test_merge_requires_explicit_conflicting_anchor_resolution_and_preserves_lineage(django_user_model):
    from apps.treatments.cycles import merge_cycles
    from apps.treatments.models import CycleLineage, TreatmentCycle, TreatmentRevision

    _, patient = _patient(django_user_model, "cycle-merge-conflict")
    one = create(patient, patient.account)
    two = create(patient, patient.account, occurred_on="2024-03-21")
    left, right = cycle(patient, [one]), cycle(patient, [two], anchor="2024-03-21")
    expected = {str(left.pk): 1, str(right.pk): 1}
    with pytest.raises(ValidationError):
        merge_cycles(patient, actor=patient.account, cycle_ids=[left.pk, right.pk], expected_revisions=expected,
                     resolution={}, checked_original=True, operation_id=uuid.uuid4())
    assert TreatmentCycle.objects.count() == 2
    operation = uuid.uuid4()
    merged = merge_cycles(patient, actor=patient.account, cycle_ids=[left.pk, right.pk], expected_revisions=expected,
                          resolution={"anchor": None, "anchor_precision": "UNKNOWN"},
                          checked_original=True, operation_id=operation)
    assert merged.current_content["anchor"] is None
    assert set(CycleLineage.objects.filter(successor=merged).values_list("predecessor_id", flat=True)) == {left.pk, right.pk}
    left.refresh_from_db()
    right.refresh_from_db()
    assert left.current_content["status"] == right.current_content["status"] == "SUPERSEDED"
    assert left.initial_content["anchor"] == "2024-02-29"
    replay = merge_cycles(patient, actor=patient.account, cycle_ids=[left.pk, right.pk], expected_revisions=expected,
                          resolution={"anchor": None, "anchor_precision": "UNKNOWN"},
                          checked_original=True, operation_id=operation)
    assert replay.pk == merged.pk
    assert TreatmentCycle.objects.count() == 3
    assert TreatmentRevision.objects.filter(operation_id=operation).count() == 3


def test_split_assigns_each_event_at_most_once_and_retains_parent(django_user_model):
    from apps.treatments.cycles import split_cycle
    from apps.treatments.models import CycleLineage, TreatmentCycle

    _, patient = _patient(django_user_model, "cycle-split-assignment")
    one, two = create(patient, patient.account), create(patient, patient.account, occurred_on="2024-03-21")
    parent = cycle(patient, [one, two])
    parts = [{"event_ids": [str(one.pk)], "anchor": "2024-02-29", "anchor_precision": "DAY", "ordinal": None},
             {"event_ids": [str(two.pk)], "anchor": "2024-03-21", "anchor_precision": "DAY", "ordinal": None}]
    with pytest.raises(ValidationError):
        split_cycle(patient, parent.pk, actor=patient.account, expected_revision=1, parts=[parts[0], parts[0]],
                    checked_original=True, operation_id=uuid.uuid4())
    children = split_cycle(patient, parent.pk, actor=patient.account, expected_revision=1, parts=parts,
                            checked_original=True, operation_id=uuid.uuid4())
    assert [c.current_content["anchor"] for c in children] == ["2024-02-29", "2024-03-21"]
    assert all(c.current_content["ordinal"] is None for c in children)
    assert set(CycleLineage.objects.filter(predecessor=parent).values_list("successor_id", flat=True)) == {c.pk for c in children}
    parent.refresh_from_db()
    assert parent.current_content["status"] == "SUPERSEDED"
    assert TreatmentCycle.objects.count() == 3


def test_one_stale_merge_revision_rolls_back_every_parent(django_user_model):
    from apps.treatments.cycles import merge_cycles
    from apps.treatments.models import TreatmentCycle
    from apps.treatments.services import TreatmentConflict

    _, patient = _patient(django_user_model, "cycle-merge-stale")
    first, second = cycle(patient, [create(patient, patient.account)]), cycle(patient, [create(patient, patient.account)])
    with pytest.raises(TreatmentConflict):
        merge_cycles(patient, actor=patient.account, cycle_ids=[first.pk, second.pk],
                     expected_revisions={str(first.pk): 1, str(second.pk): 0}, resolution={},
                     checked_original=True, operation_id=uuid.uuid4())
    assert TreatmentCycle.objects.count() == 2
    assert all(c.current_content["status"] == "CONFIRMED" and c.revision_number == 1 for c in TreatmentCycle.objects.all())


@pytest.mark.parametrize("action,expected_status", [("REVOKE", "PENDING"), ("REJECT", "REJECTED")])
def test_revoke_and_reject_remove_cycles_from_confirmed_output(django_user_model, action, expected_status):
    from apps.treatments.cycles import revise_cycle

    _, patient = _patient(django_user_model, "cycle-decision-state")
    item = cycle(patient, [create(patient, patient.account)])
    revise_cycle(patient, item.pk, actor=patient.account, action=action, expected_revision=1, operation_id=uuid.uuid4())
    row = treatment_material(patient, actor=patient.account)["cycles"][0]
    assert row["status"] == expected_status and not row["usable"]


def test_read_only_member_cannot_merge_or_split_cycles(django_user_model):
    from apps.treatments.cycles import merge_cycles, split_cycle

    _, patient = _patient(django_user_model, "cycle-role-owner")
    _, other = _patient(django_user_model, "cycle-role-reader")
    PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    item = cycle(patient, [create(patient, patient.account)])
    with pytest.raises(PermissionDenied):
        merge_cycles(patient, actor=other.account, cycle_ids=[item.pk], expected_revisions={}, resolution={},
                     checked_original=True, operation_id=uuid.uuid4())
    with pytest.raises(PermissionDenied):
        split_cycle(patient, item.pk, actor=other.account, expected_revision=1, parts=[], checked_original=True, operation_id=uuid.uuid4())
