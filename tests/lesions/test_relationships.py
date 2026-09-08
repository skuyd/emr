from apps.facts.laterality import review_parent_arguments
import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.lesions.models import Lesion, LesionObservation, LesionOperation
from apps.lesions.readmodels import review_observations
from apps.lesions.services import create_lesion, match_observations, rename_lesion, split_observations, undo_operation, unlink_observations
from .factories import imaging_observation


pytestmark = pytest.mark.django_db


def test_review_lists_local_observations_with_field_sources_without_writing_graph(django_user_model):
    patient, document, report = imaging_observation(django_user_model)
    rows = review_observations(patient, actor=patient.account)
    assert len(rows) == 1
    row = rows[0]
    assert row["source_usable"] and not row["usable"] and row["status"] == "UNASSIGNED"
    assert row["report_id"] == str(report.pk) and row["document_id"] == str(document.pk)
    assert row["site"] == ["左肺上叶"]
    assert all(field["fragments"] and field["source_valid"] for field in row["fields"])
    assert review_observations(patient, actor=patient.account)[0]["id"] == row["id"]
    assert (Lesion.objects.count(), LesionObservation.objects.count(), LesionOperation.objects.count()) == (0, 0, 0)


def test_user_creates_stable_identity_with_immutable_source_bound_assignment(django_user_model):
    patient, _, report = imaging_observation(django_user_model, name="lesion-create")
    rows = review_observations(patient, actor=patient.account)
    assert len(rows) == 1
    row = rows[0]
    operation = create_lesion(patient, actor=patient.account, observation_id=row["id"], expected_revision=0,
                              expected_source=row["source_token"], name="左上叶观察 A", checked_original=True)
    assert Lesion.objects.count() == 1
    identity = Lesion.objects.get()
    current = review_observations(patient, actor=patient.account)[0]
    assert current["usable"] and current["status"] == "CONFIRMED"
    assert current["lesion_id"] == str(identity.pk) and current["lesion_name"] == "左上叶观察 A"
    assert operation.author_id == patient.account_id and operation.checked_original
    revision = LesionObservation.objects.get().revisions.get()
    assert revision.operation_id == operation.pk
    assert revision.after["source_binding"] == row["source_binding"]
    assert revision.observation.original_report_id == report.pk
    assert identity.original_name == "左上叶观察 A"


def expectations(rows):
    return {row["id"]: {"revision": row["revision_number"], "source": row["source_token"]} for row in rows}


def test_renaming_keeps_original_name_and_explicit_author_revision(django_user_model):
    patient, _, _ = imaging_observation(django_user_model, name="lesion-rename")
    row = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=row["id"], expected_revision=0,
                  expected_source=row["source_token"], name="观察 A", checked_original=True)
    lesion = Lesion.objects.get()
    operation = rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                              expected_revision=lesion.revision_number, name="随访观察 A")
    assert review_observations(patient, actor=patient.account)[0]["lesion_name"] == "随访观察 A"
    lesion.refresh_from_db()
    assert lesion.original_name == "观察 A" and lesion.revision_number == 2
    revision = lesion.revisions.get(operation=operation)
    assert revision.before["name"] == "观察 A" and revision.after["name"] == "随访观察 A"
    assert operation.author_id == patient.account_id


def test_explicit_two_report_match_is_one_atomic_audited_operation(django_user_model):
    patient, _, _ = imaging_observation(django_user_model, name="lesion-match")
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    rows = review_observations(patient, actor=patient.account)
    assert len(rows) == 2 and all(not row["usable"] for row in rows)
    operation = match_observations(patient, actor=patient.account, first_id=rows[0]["id"], second_id=rows[1]["id"],
                                   expectations=expectations(rows), checked_original=True, name="观察 A")
    current = review_observations(patient, actor=patient.account)
    assert all(row["usable"] for row in current)
    assert len({row["lesion_id"] for row in current}) == 1
    assert len({row["report_id"] for row in current}) == 2
    assert operation.action == "MATCH" and operation.author_id == patient.account_id
    assert operation.observation_revisions.count() == 2 and operation.lesion_revisions.count() == 1
    for revision in operation.observation_revisions.all():
        assert revision.before["status"] == "UNASSIGNED"
        assert revision.after["source_binding"] in [row["source_binding"] for row in rows]


def matched_pair(django_user_model, name):
    patient, _, _ = imaging_observation(django_user_model, name=name)
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    rows = review_observations(patient, actor=patient.account)
    operation = match_observations(patient, actor=patient.account, first_id=rows[0]["id"], second_id=rows[1]["id"],
                                   expectations=expectations(rows), checked_original=True, name="观察 A")
    return patient, operation, review_observations(patient, actor=patient.account)


def test_undo_match_reverses_all_assignments_without_deleting_identity_or_original_decision(django_user_model):
    patient, original, rows = matched_pair(django_user_model, "lesion-undo")
    lesion_id = rows[0]["lesion_id"]
    reversal = undo_operation(patient, actor=patient.account, operation_id=original.pk)
    current = review_observations(patient, actor=patient.account)
    assert all(row["status"] == "UNASSIGNED" and row["lesion_id"] is None for row in current)
    assert Lesion.objects.filter(pk=lesion_id).exists()
    assert original.observation_revisions.count() == 2
    assert reversal.reverses_id == original.pk and reversal.observation_revisions.count() == 2
    assert LesionOperation.objects.count() == 2
    with pytest.raises(ValidationError):
        undo_operation(patient, actor=patient.account, operation_id=original.pk)


def test_only_current_patient_writer_can_create_and_actual_actor_is_retained(django_user_model):
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    patient, _, _ = imaging_observation(django_user_model, name="lesion-access")
    _, other = _patient(django_user_model, "lesion-other")
    row = review_observations(patient, actor=patient.account)[0]
    arguments = dict(observation_id=row["id"], expected_revision=0, expected_source=row["source_token"],
                     name="观察 A", checked_original=True)
    with pytest.raises(PermissionDenied):
        create_lesion(patient, actor=other.account, **arguments)
    member = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    assert len(review_observations(patient, actor=other.account)) == 1
    with pytest.raises(PermissionDenied):
        create_lesion(patient, actor=other.account, **arguments)
    member.role = "EDITOR"
    member.save(update_fields=["role"])
    operation = create_lesion(patient, actor=other.account, **arguments)
    assert operation.author_id == other.account_id
    assert Lesion.objects.get().created_by_id == other.account_id


def test_creation_requires_original_review_and_current_revision_without_partial_writes(django_user_model):
    patient, _, _ = imaging_observation(django_user_model, name="lesion-invalid-create")
    row = review_observations(patient, actor=patient.account)[0]
    arguments = dict(observation_id=row["id"], expected_revision=0, expected_source=row["source_token"],
                     name="观察 A", checked_original=True)
    for changes in ({"checked_original": False}, {"checked_original": "true"},
                    {"expected_revision": True}, {"expected_source": "stale"}):
        with pytest.raises(ValidationError):
            create_lesion(patient, actor=patient.account, **{**arguments, **changes})
    assert (Lesion.objects.count(), LesionObservation.objects.count(), LesionOperation.objects.count()) == (0, 0, 0)


def test_parent_report_exclusion_and_restore_do_not_revive_relation(django_user_model):
    from apps.facts.clinical_readmodels import report_state
    from apps.facts.clinical_services import revise_report

    patient, _, report = imaging_observation(django_user_model, name="lesion-parent")
    row = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=row["id"], expected_revision=0,
                  expected_source=row["source_token"], name="观察 A", checked_original=True)
    for action in ("EXCLUDE", "UNDO"):
        report.refresh_from_db()
        state = report_state(report)
        revise_report(patient, actor=patient.account, report_id=report.pk, action=action,
                      expected_revision=report.revision_number, expected_source=state["current_source_token"])
        assert not review_observations(patient, actor=patient.account)[0]["usable"]
    current = review_observations(patient, actor=patient.account)[0]
    assert current["source_usable"] and current["status"] == "STALE"


def test_original_identity_and_operation_revision_cannot_be_overwritten(django_user_model):
    patient, original, rows = matched_pair(django_user_model, "lesion-immutable")
    revision = original.observation_revisions.first()
    revision.after["status"] = "UNASSIGNED"
    with pytest.raises(ValidationError):
        revision.save()
    original.action = "OTHER"
    with pytest.raises(ValidationError):
        original.save()
    lesion = Lesion.objects.get()
    lesion.original_name = "覆盖"
    with pytest.raises(ValidationError):
        lesion.save()
    assert all(row["usable"] for row in review_observations(patient, actor=patient.account))


def test_selected_observation_split_can_be_undone_as_one_complete_operation(django_user_model):
    patient, _, rows = matched_pair(django_user_model, "lesion-split")
    original = Lesion.objects.get()
    selected = rows[1]
    operation = split_observations(patient, actor=patient.account, lesion_id=original.pk,
                                   expected_revision=original.revision_number, observation_ids=[selected["id"]],
                                   expectations=expectations([selected]), name="独立观察 B", checked_original=True)
    current = {row["id"]: row for row in review_observations(patient, actor=patient.account)}
    assert current[selected["id"]]["lesion_id"] != str(original.pk)
    assert current[rows[0]["id"]]["lesion_id"] == str(original.pk)
    assert operation.lesion_revisions.count() == 2 and operation.observation_revisions.count() == 1
    undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    restored = review_observations(patient, actor=patient.account)
    assert {row["lesion_id"] for row in restored} == {str(original.pk)}
    assert all(row["usable"] for row in restored)


def test_undo_cannot_restore_old_confirmation_after_field_revoke_and_restore(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    patient, _, rows = matched_pair(django_user_model, "lesion-stale-undo")
    original = Lesion.objects.get()
    selected = rows[1]
    operation = split_observations(patient, actor=patient.account, lesion_id=original.pk,
                                   expected_revision=original.revision_number, observation_ids=[selected["id"]],
                                   expectations=expectations([selected]), name="观察 B", checked_original=True)
    assert operation is not None
    field = Fact.objects.get(pk=next(f["id"] for f in selected["fields"] if f["field_key"] == "lesion.site"))
    for action in ("REVOKE", "UNDO"):
        field.refresh_from_db()
        state = effective_fact(field)
        revise_fact(patient, field.pk, actor=patient.account, action=action, expected_revision=field.revision_number,
                    expected_source=state["current_source_token"], checked_original=True, **review_parent_arguments(field))
    field.refresh_from_db()
    assert effective_fact(field)["usable"]
    current = next(row for row in review_observations(patient, actor=patient.account) if row["id"] == selected["id"])
    assert not current["usable"] and current["status"] == "STALE"
    with pytest.raises(ValidationError, match="来源"):
        undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    assert LesionOperation.objects.count() == 2


def test_reassign_keeps_two_original_identities_and_restores_both_on_undo(django_user_model):
    patient, _, _ = imaging_observation(django_user_model, name="lesion-reassign")
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    for row, name in zip(review_observations(patient, actor=patient.account), ("观察 A", "观察 B")):
        create_lesion(patient, actor=patient.account, observation_id=row["id"], expected_revision=0,
                      expected_source=row["source_token"], name=name, checked_original=True)
    original = review_observations(patient, actor=patient.account)
    target = original[1]["lesion_id"]
    versions = {str(lesion.pk): lesion.revision_number for lesion in Lesion.objects.all()}
    operation = match_observations(patient, actor=patient.account, first_id=original[0]["id"], second_id=original[1]["id"],
                                   expectations=expectations(original), checked_original=True, target_lesion_id=target,
                                   expected_lesion_revisions=versions)
    assert operation.action == "REASSIGN" and operation.lesion_revisions.count() == 2
    assert {row["lesion_id"] for row in review_observations(patient, actor=patient.account)} == {target}
    assert Lesion.objects.count() == 2
    undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    assert [(row["id"], row["lesion_id"]) for row in review_observations(patient, actor=patient.account)] == [
        (row["id"], row["lesion_id"]) for row in original]


def test_unlink_is_source_checked_and_undo_restores_assignment_without_deleting_history(django_user_model):
    patient, _, rows = matched_pair(django_user_model, "lesion-unlink")
    lesion = Lesion.objects.get()
    selected = rows[1]
    operation = unlink_observations(patient, actor=patient.account, lesion_id=lesion.pk,
                                    expected_revision=lesion.revision_number, observation_ids=[selected["id"]],
                                    expectations=expectations([selected]))
    current = {row["id"]: row for row in review_observations(patient, actor=patient.account)}
    assert current[selected["id"]]["lesion_id"] is None and current[selected["id"]]["status"] == "UNASSIGNED"
    assert current[rows[0]["id"]]["usable"]
    assert operation.observation_revisions.get().before["lesion_id"] == str(lesion.pk)
    undo_operation(patient, actor=patient.account, operation_id=operation.pk)
    assert all(row["usable"] for row in review_observations(patient, actor=patient.account))


def test_unavailable_source_may_be_unlinked_but_undo_must_not_restore_old_confirmation(django_user_model):
    from apps.facts.clinical_readmodels import report_state
    from apps.facts.clinical_services import revise_report
    from apps.facts.models import ClinicalReport

    patient, _, rows = matched_pair(django_user_model, "lesion-unavailable-unlink")
    lesion = Lesion.objects.get()
    selected = rows[1]
    report = ClinicalReport.objects.get(pk=selected["report_id"])
    revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=report.revision_number,
                   expected_source=report_state(report)["current_source_token"])
    selected = next(row for row in review_observations(patient, actor=patient.account, include_unavailable=True)
                    if row["id"] == selected["id"])
    operation = unlink_observations(patient, actor=patient.account, lesion_id=lesion.pk,
                                    expected_revision=lesion.revision_number, observation_ids=[selected["id"]],
                                    expectations=expectations([selected]))
    assert operation is not None and operation.observation_revisions.get().after["lesion_id"] is None
    with pytest.raises(ValidationError, match="来源"):
        undo_operation(patient, actor=patient.account, operation_id=operation.pk)
