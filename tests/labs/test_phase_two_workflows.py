from copy import copy
from datetime import date
from decimal import Decimal
import uuid

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.utils import timezone
import pytest

from apps.documents.deletion import request_document_deletion
from apps.documents.models import ProcessingRun, ProcessingStage
from apps.labs import models
from apps.processing.models import ParsingVersion, ParsingVersionStatus, SourceEvidence
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


@pytest.fixture
def case(django_user_model):
    client, patient = _patient(django_user_model, "p2-owner")
    document, row = _observation(patient, date(2026, 8, 20), "62")
    return client, patient, document, row


def _reviewer(django_user_model):
    actor = django_user_model.objects.create(phone_hash=uuid.uuid4().hex * 2, phone_encrypted="cipher", is_staff=True)
    actor.user_permissions.add(Permission.objects.get(codename="review_labobservation"))
    return actor


def _withdraw_reviewer_authority(reviewer, withdrawal):
    if withdrawal == "permission":
        reviewer.user_permissions.clear()
    else:
        type(reviewer).objects.filter(pk=reviewer.pk).update(**{withdrawal: False})


@pytest.mark.parametrize("withdrawal", ["is_staff", "permission", "is_active"])
@pytest.mark.parametrize("operation", ["create", "assign"])
def test_review_grant_rechecks_cached_reviewer_after_lock(case, django_user_model, monkeypatch, withdrawal, operation):
    from django.core.exceptions import ValidationError
    from apps.labs import review

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    task = review.create_review_task(patient.account, row.pk) if operation == "assign" else None
    assert reviewer.has_perm("labs.review_labobservation")
    real_lock = review.lock_observation

    def revoke_after_lock(identity):
        locked = real_lock(identity)
        _withdraw_reviewer_authority(reviewer, withdrawal)
        return locked

    monkeypatch.setattr(review, "lock_observation", revoke_after_lock)
    with pytest.raises(ValidationError):
        if operation == "create":
            review.create_review_task(patient.account, row.pk, reviewer=reviewer)
        else:
            review.assign_review_task(patient.account, task.pk, reviewer=reviewer, expected_revision=0)
    if task is None:
        assert not row.review_tasks.exists()
    else:
        task.refresh_from_db()
        assert task.reviewer_id is None
        assert task.revision_number == 0
        assert list(task.events.values_list("action", flat=True)) == ["CREATE"]


@pytest.mark.parametrize("operation", ["create", "assign", "revoke"])
def test_review_mutations_recheck_owner_activation_after_lock(case, django_user_model, monkeypatch, operation):
    from apps.labs import review

    _client, patient, _document, row = case
    owner = patient.account
    reviewer = _reviewer(django_user_model)
    task = review.create_review_task(owner, row.pk) if operation != "create" else None
    real_lock = review.lock_observation

    def deactivate_after_lock(identity):
        locked = real_lock(identity)
        type(owner).objects.filter(pk=owner.pk).update(is_active=False)
        return locked

    monkeypatch.setattr(review, "lock_observation", deactivate_after_lock)
    with pytest.raises(PermissionDenied):
        if operation == "create":
            review.create_review_task(owner, row.pk, reviewer=reviewer)
        elif operation == "assign":
            review.assign_review_task(owner, task.pk, reviewer=reviewer, expected_revision=0)
        else:
            review.transition_review_task(owner, task.pk, action="REVOKE", expected_revision=0)
    if task is None:
        assert not row.review_tasks.exists()
    else:
        task.refresh_from_db()
        assert task.revision_number == 0
        assert task.status == "PENDING"
        assert task.events.count() == 1


@pytest.mark.parametrize("withdrawal", ["is_staff", "permission", "is_active"])
def test_review_service_rechecks_cached_actor_before_submission(case, django_user_model, withdrawal):
    from apps.labs import review

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    task = review.create_review_task(patient.account, row.pk, reviewer=reviewer)
    review.transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    review.get_review_task(reviewer, task.pk)
    _withdraw_reviewer_authority(reviewer, withdrawal)

    with pytest.raises(PermissionDenied):
        review.transition_review_task(reviewer, task.pk, action="CONFIRM", expected_revision=1)
    row.refresh_from_db()
    task.refresh_from_db()
    assert row.revision_number == 0
    assert not row.revisions.exists()
    assert task.status == "IN_PROGRESS"
    assert task.revision_number == 1
    assert task.events.count() == 2


def _new_version(document, row, *, raw_value="63"):
    run = ProcessingRun.objects.create(
        document=document, parser_version="parser-p2", task_type="reparse",
        idempotency_key=uuid.uuid4().hex, stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(), is_current=False, attempt_number=document.processing_runs.count() + 1,
    )
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="parser-p2", ocr_provider="fixture",
        ocr_provider_version="1", dictionary_version=row.dictionary_version, dictionary_hash="a" * 64,
        status=ParsingVersionStatus.READY,
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version, document_page=row.document_page,
        source_text=row.evidence.source_text, polygon=row.evidence.polygon, confidence="0.9800",
    )
    new = copy(row)
    new.pk = uuid.uuid4()
    new._state = copy(row._state)
    new._state.adding = True
    new.parsing_version = version
    new.evidence = evidence
    new.raw_value = raw_value
    new.revision_number = 0
    new.save(force_insert=True)
    ParsingVersion.objects.activate(version)
    return new


def test_phase_two_persists_separate_quality_and_revision_state():
    fields = {field.name for field in models.LabObservation._meta.fields}
    assert {"specimen", "field_evidence", "quality_issues", "reference_range", "revision_number"} <= fields
    assert hasattr(models, "ObservationRevision")
    assert hasattr(models, "ReviewTask")


def test_correction_and_undo_preserve_raw_values_and_author(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, _document, row = case
    event = revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    row.refresh_from_db()
    effective = effective_observation(row)
    assert row.raw_value == "62"
    assert effective.raw_value == "6.2"
    assert effective.result_type == "NUMERIC"
    assert effective.value_origin == "USER"
    assert event.author_id == patient.account_id
    assert event.before["raw_value"] == "62"
    assert event.after["raw_value"] == "6.2"
    assert event.source_evidence_id == row.evidence_id
    revise_observation(patient.account, row.pk, action="UNDO", changes={}, expected_revision=1)
    row.refresh_from_db()
    assert effective_observation(row).raw_value == "62"
    assert row.revisions.count() == 2


def test_confirmation_does_not_clear_unit_or_date_restrictions(case):
    from apps.labs.revisions import effective_observation, revise_observation
    from apps.labs.validation import validate_observation

    _client, patient, _document, row = case
    row.raw_unit = "mg/dL"
    row.observation_date = None
    row.save(update_fields=["raw_unit", "observation_date"])
    revise_observation(patient.account, row.pk, action="CONFIRM", changes={}, expected_revision=0)
    row.refresh_from_db()
    effective = effective_observation(row)
    assert effective.review_state == "CONFIRM"
    codes = {issue["code"] for issue in validate_observation(effective)}
    assert {"unit_unknown", "date_uncertain"} <= codes
    assert row.evidence.confidence == Decimal("0.9800")


def test_feedback_needs_no_correct_answer_and_defer_is_nonblocking(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, _document, row = case
    revise_observation(patient.account, row.pk, action="REPORT_ERROR", changes={}, expected_revision=0)
    row.refresh_from_db()
    assert effective_observation(row).review_state == "REPORT_ERROR"
    assert effective_observation(row).raw_value == "62"
    revise_observation(patient.account, row.pk, action="DEFER", changes={}, expected_revision=1)
    row.refresh_from_db()
    assert effective_observation(row).reported_error is True


def test_revision_denies_other_owner_and_stale_concurrent_write(case, django_user_model):
    from apps.labs.revisions import RevisionConflict, revise_observation

    _client, patient, _document, row = case
    _other_client, other = _patient(django_user_model, "p2-other")
    with pytest.raises(PermissionDenied):
        revise_observation(other.account, row.pk, action="CONFIRM", changes={}, expected_revision=0)
    revise_observation(patient.account, row.pk, action="CONFIRM", changes={}, expected_revision=0)
    with pytest.raises(RevisionConflict):
        revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "0"}, expected_revision=0)
    assert row.revisions.count() == 1


def test_review_requires_task_grant_and_revocation_removes_access(case, django_user_model):
    from apps.labs.review import create_review_task, get_review_task, transition_review_task

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    outsider = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    assert task.status == "PENDING"
    with pytest.raises(PermissionDenied):
        get_review_task(outsider, task.pk)
    assert get_review_task(reviewer, task.pk).pk == task.pk
    task = transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    assert task.status == "IN_PROGRESS"
    task = transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=1)
    assert task.status == "REVOKED"
    for actor in (reviewer, outsider):
        with pytest.raises(PermissionDenied):
            get_review_task(actor, task.pk)
        with pytest.raises(PermissionDenied):
            transition_review_task(actor, task.pk, action="CONFIRM", expected_revision=2)
    assert task.events.count() == 3


def test_review_correction_is_separate_from_ocr_and_has_optimistic_guard(case, django_user_model):
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import RevisionConflict, effective_observation

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    task = transition_review_task(reviewer, task.pk, action="CORRECT", changes={"raw_value": "<6.2"}, expected_revision=1)
    row.refresh_from_db()
    effective = effective_observation(row)
    assert effective.raw_value == "<6.2"
    assert effective.result_type == "COMPARATOR"
    assert effective.value_origin == "REVIEW"
    assert task.status == "COMPLETED"
    assert row.evidence.confidence == Decimal("0.9800")
    with pytest.raises(RevisionConflict):
        transition_review_task(reviewer, task.pk, action="CORRECT", changes={"raw_value": "2"}, expected_revision=1)


def test_owner_correction_makes_an_open_review_submission_stale(case, django_user_model):
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import RevisionConflict, revise_observation

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    with pytest.raises(RevisionConflict):
        transition_review_task(reviewer, task.pk, action="CONFIRM", expected_revision=1)


def test_reparse_preserves_human_revision_and_surfaces_conflict(case, django_user_model):
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import RevisionConflict, effective_observation, revise_observation

    _client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    new = _new_version(document, row)
    effective = effective_observation(new)
    assert effective.revision_conflict is True
    assert effective.raw_value == "6.2"
    assert effective.original_observation_id == row.pk
    assert new.raw_value == "63"
    with pytest.raises(RevisionConflict):
        transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    ParsingVersion.objects.activate(row.parsing_version_id)
    row.refresh_from_db()
    assert effective_observation(row).raw_value == "6.2"
    assert effective_observation(row).revision_conflict is False


def test_deletion_revokes_tasks_and_prevents_revision_writes(case, django_user_model):
    from apps.labs.review import create_review_task, get_review_task
    from apps.labs.revisions import revise_observation

    _client, patient, document, row = case
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    request_document_deletion(patient, document.pk, dispatch=lambda _job: None)
    task.refresh_from_db()
    assert task.status == "REVOKED"
    with pytest.raises(PermissionDenied):
        get_review_task(reviewer, task.pk)
    with pytest.raises(PermissionDenied):
        revise_observation(patient.account, row.pk, action="CONFIRM", changes={}, expected_revision=0)


def test_review_resolution_is_reset_after_another_value_edit(case, django_user_model):
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import effective_observation, revise_observation
    from apps.labs.validation import validate_observation

    _client, patient, _document, row = case
    row.evidence.confidence = "0.8300"
    row.evidence.save()
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    transition_review_task(reviewer, task.pk, action="CONFIRM", expected_revision=1, resolved_issues=["recognition_uncertain"])
    row.refresh_from_db()
    assert "recognition_uncertain" not in {i["code"] for i in validate_observation(effective_observation(row))}
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=1)
    row.refresh_from_db()
    assert "recognition_uncertain" in {i["code"] for i in validate_observation(effective_observation(row))}


def test_review_grant_expiration_and_role_removal_stop_reads(case, django_user_model):
    from datetime import timedelta
    from apps.labs.review import create_review_task, get_review_task

    _client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    task.expires_at = timezone.now() - timedelta(seconds=1)
    task.save()
    with pytest.raises(PermissionDenied):
        get_review_task(reviewer, task.pk)
    task.expires_at = timezone.now() + timedelta(days=1)
    task.save()
    reviewer.user_permissions.clear()
    reviewer = django_user_model.objects.get(pk=reviewer.pk)
    with pytest.raises(PermissionDenied):
        get_review_task(reviewer, task.pk)


def test_field_date_and_project_corrections_keep_auditable_values(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, _document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={
        "observation_date": "2026-08-19", "standard_code": "LAB_HGB", "raw_unit": "g/L", "raw_value": "130",
    }, expected_revision=0)
    row.refresh_from_db()
    value = effective_observation(row)
    assert value.standard_code == "LAB_HGB"
    assert value.observation_date == date(2026, 8, 19)
    assert value.raw_unit == "g/L"
    assert value.raw_value == "130"
    assert row.standard_code == "LAB_WBC"


def test_revision_continuity_uses_version_lineage_even_with_equal_timestamps(django_user_model):
    from freezegun import freeze_time
    from apps.labs.revisions import effective_observation, revise_observation

    with freeze_time("2026-09-06 08:00:00"):
        _client, patient = _patient(django_user_model, "p2-equal-version-time")
        document, row = _observation(patient, date(2026, 8, 20), "62")
        revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
        new = _new_version(document, row)
        effective = effective_observation(new)
        assert effective.raw_value == "6.2"
        assert effective.revision_conflict is True


def test_reparse_preserves_project_correction_at_same_source(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={
        "standard_code": "LAB_HGB", "raw_value": "130", "raw_unit": "g/L",
    }, expected_revision=0)
    new = _new_version(document, row, raw_value="131")
    new.standard_code, new.standard_name, new.raw_unit = "LAB_HGB", "血红蛋白", "g/L"
    new.save()
    result = effective_observation(new)
    assert result.raw_value == "130"
    assert result.standard_code == "LAB_HGB"
    assert result.value_origin == "USER"
    assert result.revision_conflict is True


def test_defer_keeps_value_source_across_two_reparses(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, document, row = case
    original_event = revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    second = _new_version(document, row)
    revise_observation(patient.account, second.pk, action="DEFER", changes={}, expected_revision=0)
    second.refresh_from_db()
    deferred = effective_observation(second)
    assert deferred.original_observation_id == row.pk
    assert deferred.carried_source_evidence.pk == row.evidence_id
    assert deferred.value_sources["raw_value"]["revision_id"] == str(original_event.pk)
    third = _new_version(document, second)
    result = effective_observation(third)
    assert result.raw_value == "6.2"
    assert result.revision_conflict is True
    assert result.original_observation_id == row.pk


def test_unrelated_edit_cannot_clear_reparse_value_conflict(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    second = _new_version(document, row)
    revise_observation(patient.account, second.pk, action="CORRECT", changes={"raw_unit": row.raw_unit}, expected_revision=0)
    second.refresh_from_db()
    assert effective_observation(second).revision_conflict is True


def test_explicit_reparse_resolution_is_reversible_and_preserves_sources(case):
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    second = _new_version(document, row)
    revise_observation(patient.account, second.pk, action="KEEP_REVISION", changes={}, expected_revision=0)
    second.refresh_from_db()
    kept = effective_observation(second)
    assert kept.raw_value == "6.2" and kept.revision_conflict is False
    assert kept.original_observation_id == row.pk
    revise_observation(patient.account, second.pk, action="UNDO", changes={}, expected_revision=1)
    second.refresh_from_db()
    assert effective_observation(second).revision_conflict is True
    revise_observation(patient.account, second.pk, action="USE_AUTOMATIC", changes={}, expected_revision=2)
    second.refresh_from_db()
    accepted = effective_observation(second)
    assert accepted.raw_value == "63" and accepted.revision_conflict is False
    assert accepted.value_origin == "AUTOMATIC"
    assert accepted.original_observation_id == second.pk


def test_missing_result_is_preserved_only_as_a_source_backed_candidate(case):
    from django.core.exceptions import ValidationError

    _client, _patient_record, _document, row = case
    row.raw_value = ""
    row.result_type = "STATUS"
    with pytest.raises(ValidationError):
        row.full_clean()
    row.capability_level = "SEARCH_ONLY"
    row.quality_issues = [{"code": "association_conflict", "detail": "Result cell is absent"}]
    row.full_clean()
    row.save()
    assert models.LabObservation.objects.get(pk=row.pk).raw_value == ""
