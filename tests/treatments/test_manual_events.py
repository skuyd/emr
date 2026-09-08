"""User treatment notes retain exact precision, authorship and append-only decisions."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient


pytestmark = pytest.mark.django_db


def create(patient, actor, **kwargs):
    from apps.treatments.services import create_manual_event

    values = dict(kind="SYSTEMIC_TREATMENT", title="合成治疗记录", occurrence="OCCURRED",
                  occurred_on="2024-02-29", date_precision="DAY", regimen_text="合成方案 A",
                  note="本人补记", operation_id=uuid.uuid4())
    values.update(kwargs)
    return create_manual_event(patient, actor=actor, **values)


def test_member_note_retains_actor_and_unknown_date_without_fabricating_source(django_user_model):
    from apps.treatments.models import TreatmentEvidence, TreatmentRevision
    from apps.treatments.readmodels import treatment_material

    _, patient = _patient(django_user_model, "treatment-note-owner")
    _, other = _patient(django_user_model, "treatment-note-editor")
    PatientMembership.objects.create(patient=patient, account=other.account, role="EDITOR")
    event = create(patient, other.account, occurred_on=None, date_precision="UNKNOWN")
    assert event.created_by_id == other.account_id
    assert event.origin == "USER" and event.initial_content["date"] is None
    assert event.initial_content["cycle_ordinal"] is None
    assert not TreatmentEvidence.objects.filter(event=event).exists()
    revision = TreatmentRevision.objects.get(event=event)
    assert revision.author_id == other.account_id and revision.action == "CREATE"
    material = treatment_material(patient, actor=other.account)
    own = next(row for row in material["events"] if row["id"] == str(event.pk))
    assert own["content"]["date_precision"] == "UNKNOWN" and own["content"]["date"] is None
    assert own["origin"] == "USER" and own["sources"] == []


@pytest.mark.parametrize("role", ["VIEWER", None])
def test_read_only_and_foreign_accounts_cannot_write_treatment_notes(django_user_model, role):
    from apps.treatments.models import TreatmentEvent

    _, patient = _patient(django_user_model, "treatment-denied-owner")
    _, other = _patient(django_user_model, "treatment-denied-actor")
    if role:
        PatientMembership.objects.create(patient=patient, account=other.account, role=role)
    with pytest.raises(PermissionDenied):
        create(patient, other.account)
    assert not TreatmentEvent.objects.exists()


@pytest.mark.parametrize("day,precision,ordinal,cycle_day", [
    ("2023-02-29", "DAY", None, None), ("2024-02", "DAY", None, None),
    (None, "DAY", None, None), ("2024-02-29", "UNKNOWN", None, None),
    ("2024-13", "MONTH", None, None), ("2024", "YEAR", True, None),
    ("2024", "YEAR", 0, None), ("2024-02-29", "DAY", 1, 0),
])
def test_invalid_or_mismatched_dates_and_labels_fail_without_writes(django_user_model, day, precision, ordinal, cycle_day):
    from apps.treatments.models import TreatmentEvent

    _, patient = _patient(django_user_model, "treatment-invalid-date")
    with pytest.raises(ValidationError):
        create(patient, patient.account, occurred_on=day, date_precision=precision,
               cycle_ordinal=ordinal, cycle_day=cycle_day)
    assert not TreatmentEvent.objects.exists()


def test_replayed_create_is_idempotent_but_changed_payload_is_a_conflict(django_user_model):
    from apps.treatments.models import TreatmentRevision
    from apps.treatments.services import TreatmentConflict

    _, patient = _patient(django_user_model, "treatment-create-replay")
    operation = uuid.uuid4()
    first = create(patient, patient.account, operation_id=operation)
    second = create(patient, patient.account, operation_id=operation)
    assert first.pk == second.pk
    assert TreatmentRevision.objects.filter(event=first).count() == 1
    with pytest.raises(TreatmentConflict):
        create(patient, patient.account, operation_id=operation, title="不同的记录")
    first.refresh_from_db()
    assert first.current_content["title"] == "合成治疗记录"


def test_correction_preserves_baseline_and_stale_revision_cannot_overwrite(django_user_model):
    from apps.treatments.models import TreatmentRevision
    from apps.treatments.services import TreatmentConflict, revise_event

    _, patient = _patient(django_user_model, "treatment-note-correction")
    event = create(patient, patient.account)
    revise_event(patient, event.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                 changes={"date": "2024-03", "date_precision": "MONTH"},
                 checked_original=True, operation_id=uuid.uuid4())
    event.refresh_from_db()
    assert event.initial_content["date"] == "2024-02-29"
    assert event.current_content["date"] == "2024-03"
    assert event.revision_number == 2
    with pytest.raises(TreatmentConflict):
        revise_event(patient, event.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                     changes={"date": "2024-03-01", "date_precision": "DAY"},
                     checked_original=True, operation_id=uuid.uuid4())
    assert list(TreatmentRevision.objects.filter(event=event).values_list("sequence", flat=True)) == [1, 2]


def test_revocation_is_rechecked_even_with_a_previously_loaded_actor(django_user_model):
    from apps.treatments.readmodels import treatment_material
    from apps.treatments.services import revise_event

    _, patient = _patient(django_user_model, "treatment-revoke-owner")
    _, other = _patient(django_user_model, "treatment-revoke-editor")
    membership = PatientMembership.objects.create(patient=patient, account=other.account, role="EDITOR")
    event = create(patient, other.account)
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    with pytest.raises(PermissionDenied):
        treatment_material(patient, actor=other.account)
    with pytest.raises(PermissionDenied):
        revise_event(patient, event.pk, actor=other.account, action="REVOKE", expected_revision=1,
                     operation_id=uuid.uuid4())
    event.refresh_from_db()
    assert event.revision_number == 1


def test_foreign_event_identity_cannot_be_revised_through_an_authorized_patient(django_user_model):
    from apps.treatments.services import revise_event

    _, patient = _patient(django_user_model, "treatment-foreign-owner")
    _, other = _patient(django_user_model, "treatment-foreign-other")
    event = create(other, other.account)
    with pytest.raises(PermissionDenied):
        revise_event(patient, event.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                     operation_id=uuid.uuid4())
    event.refresh_from_db()
    assert event.revision_number == 1
