"""Phase Two races against real PostgreSQL row locks and persisted results.

Run with --ds=config.settings.postgres_test and an isolated
PHR_POSTGRES_TEST_URL. Every race observes pg_blocking_pids before releasing
the first transaction; timeouts turn lock-order inversions into bounded failures.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
import threading
import time
from types import SimpleNamespace
import uuid

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
import pytest

from apps.documents.deletion import request_document_deletion
from apps.documents.locking import lock_document_aggregate
from apps.documents.models import ProcessingRun, ProcessingStage
from apps.labs.dictionary import current_dictionary, dictionary_for_version
from apps.labs.dictionary_workflow import (
    collect_dictionary_candidates,
    get_dictionary_candidate,
    preview_dictionary,
    publish_dictionary,
    review_candidate,
    rollback_dictionary,
)
from apps.labs.models import DictionaryCandidate, LabObservation
from apps.labs.review import create_review_task, get_review_task, transition_review_task
from apps.labs.revisions import RevisionConflict, effective_observation, revise_observation
from apps.operations.models import DictionaryRelease
from apps.operations.permissions import Role
from apps.patients.models import Patient
from apps.processing.models import ParsingVersion, ParsingVersionStatus, SourceEvidence
from tests.labs.test_trends import _observation
from tests.operations.test_services import staff


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Run with --ds=config.settings.postgres_test and PHR_POSTGRES_TEST_URL")


def _patient(django_user_model):
    owner = django_user_model.objects.create(phone_hash=uuid.uuid4().hex * 2, phone_encrypted="synthetic")
    return Patient.objects.create(account=owner, display_name="合成并发验收")


@pytest.fixture
def case(django_user_model):
    patient = _patient(django_user_model)
    document, row = _observation(patient, date(2026, 8, 20), "62")
    reviewer = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2, phone_encrypted="synthetic", is_staff=True,
    )
    reviewer.user_permissions.add(Permission.objects.get(codename="review_labobservation"))
    return SimpleNamespace(patient=patient, owner=patient.account, document=document, row=row, reviewer=reviewer)


def _started_task(case):
    task = create_review_task(case.owner, case.row.pk, reviewer=case.reviewer)
    return transition_review_task(case.reviewer, task.pk, action="START", expected_revision=0)


def _race(first, second, *, lock_table):
    """Hold a lock taken by real service code, then prove the other backend waits."""
    locked = threading.Event()
    release = threading.Event()
    second_connected = threading.Event()
    backend_ids = {}

    def worker(label, action):
        close_old_connections()
        paused = False

        def pause_after_lock(execute, sql, params, many, context):
            nonlocal paused
            result = execute(sql, params, many, context)
            if label == "first" and not paused and f'FROM "{lock_table}"' in sql and "FOR UPDATE" in sql:
                paused = True
                locked.set()
                assert release.wait(15), "The competing transaction never reached its lock"
            return result

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '20s'")
                cursor.execute("SET idle_in_transaction_session_timeout = '30s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend_ids[label] = cursor.fetchone()[0]
            if label == "second":
                second_connected.set()
            with connection.execute_wrapper(pause_after_lock):
                try:
                    return action()
                except (PermissionDenied, RevisionConflict) as error:
                    return error
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(worker, "first", first)
        try:
            assert locked.wait(10), "First operation did not acquire the expected aggregate lock"
            second_result = executor.submit(worker, "second", second)
            assert second_connected.wait(10), "Second backend did not connect"
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [backend_ids["second"]])
                    blocked = backend_ids["first"] in cursor.fetchone()[0]
                if blocked or second_result.done():
                    break
                time.sleep(0.02)
            assert blocked, "No real overlapping PostgreSQL lock wait was observed"
        finally:
            release.set()
        return first_result.result(30), second_result.result(30)


def _review_correction(case, task):
    return transition_review_task(
        case.reviewer, task.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=1,
    )


def _ready_reparse(case):
    run = ProcessingRun.objects.create(
        document=case.document, parser_version="phase-two-concurrency", task_type="reparse",
        idempotency_key=uuid.uuid4().hex, stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(), is_current=False,
        attempt_number=case.document.processing_runs.count() + 1,
    )
    version = ParsingVersion.objects.create(
        document=case.document, processing_run=run, parser_version="phase-two-concurrency",
        ocr_provider="synthetic", ocr_provider_version="1", dictionary_version=case.row.dictionary_version,
        dictionary_hash="a" * 64, status=ParsingVersionStatus.READY,
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version, document_page=case.row.document_page,
        source_text=case.row.evidence.source_text, polygon=case.row.evidence.polygon, confidence="0.9800",
    )
    fields = {field.attname: getattr(case.row, field.attname)
              for field in LabObservation._meta.concrete_fields if not field.primary_key}
    fields.update(parsing_version_id=version.pk, evidence_id=evidence.pk, raw_value="63", revision_number=0)
    return LabObservation.objects.create(**fields)


def test_postgresql_two_owner_corrections_accept_only_one_revision(case):
    # Removing the revision CAS would let the stale writer replace the accepted correction.
    def correct(value):
        return revise_observation(case.owner, case.row.pk, action="CORRECT",
                                  changes={"raw_value": value}, expected_revision=0)

    accepted, stale = _race(lambda: correct("6.2"), lambda: correct("6.3"), lock_table="documents_uploadbatch")

    assert accepted.sequence == 1
    assert isinstance(stale, RevisionConflict)
    case.row.refresh_from_db()
    assert case.row.raw_value == "62"
    assert case.row.revision_number == 1
    assert list(case.row.revisions.values_list("sequence", "after__raw_value")) == [(1, "6.2")]
    assert effective_observation(case.row).raw_value == "6.2"
    assert accepted.source_evidence_id == case.row.evidence_id


@pytest.mark.parametrize("withdrawal", ["is_staff", "permission", "is_active"])
def test_postgresql_review_role_revocation_blocks_waiting_cached_actor(case, withdrawal):
    from tests.labs.test_phase_two_workflows import _withdraw_reviewer_authority

    task = _started_task(case)
    # Match the HTTP preflight that caches permissions before the service waits.
    get_review_task(case.reviewer, task.pk)

    def withdraw():
        with transaction.atomic():
            lock_document_aggregate(case.document.pk)
            _withdraw_reviewer_authority(case.reviewer, withdrawal)

    _withdrawn, denied = _race(withdraw, lambda: _review_correction(case, task), lock_table="documents_uploadbatch")

    assert isinstance(denied, PermissionDenied)
    case.row.refresh_from_db()
    task.refresh_from_db()
    assert case.row.revision_number == 0
    assert not case.row.revisions.exists()
    assert task.status == "IN_PROGRESS"
    assert task.revision_number == 1
    assert list(task.events.order_by("sequence").values_list("action", flat=True)) == ["CREATE", "START"]


@pytest.mark.parametrize("withdrawal", ["revoke", "delete"])
def test_postgresql_withdrawal_blocks_waiting_review_submission(case, withdrawal):
    # Authorization checked before the document lock would allow a write after withdrawal commits.
    task = _started_task(case)

    def withdraw():
        if withdrawal == "revoke":
            return transition_review_task(case.owner, task.pk, action="REVOKE", expected_revision=1)
        return request_document_deletion(case.patient, case.document.pk, dispatch=lambda _job: None)

    _withdrawn, denied = _race(withdraw, lambda: _review_correction(case, task), lock_table="documents_uploadbatch")

    assert isinstance(denied, PermissionDenied)
    case.row.refresh_from_db()
    task.refresh_from_db()
    assert task.status == "REVOKED"
    assert task.revoked_at is not None
    assert case.row.revision_number == 0
    assert not case.row.revisions.exists()
    assert list(task.events.order_by("sequence").values_list("action", flat=True)) == [
        "CREATE", "START", "REVOKE" if withdrawal == "revoke" else "DOCUMENT_DELETED",
    ]
    with pytest.raises(PermissionDenied):
        get_review_task(case.reviewer, task.pk)


def test_postgresql_deletion_waits_for_review_without_inverting_patient_and_batch_locks(case):
    # An inverted patient/batch/document lock order would deadlock this opposite arrival order.
    task = _started_task(case)
    accepted, job = _race(
        lambda: _review_correction(case, task),
        lambda: request_document_deletion(case.patient, case.document.pk, dispatch=lambda _job: None),
        lock_table="documents_uploadbatch",
    )

    assert accepted.status == "COMPLETED"
    assert job.document_id == case.document.pk
    case.document.refresh_from_db()
    case.row.refresh_from_db()
    task.refresh_from_db()
    assert case.document.deleted_at is not None
    assert task.status == "REVOKED"
    assert case.row.revision_number == 1
    assert list(task.events.order_by("sequence").values_list("action", flat=True)) == [
        "CREATE", "START", "CORRECT", "DOCUMENT_DELETED",
    ]
    with pytest.raises(PermissionDenied):
        get_review_task(case.reviewer, task.pk)
    with pytest.raises(PermissionDenied):
        revise_observation(case.owner, case.row.pk, action="CONFIRM", changes={}, expected_revision=1)


def test_postgresql_review_completion_rejects_stale_revocation_then_allows_fresh_revocation(case):
    task = _started_task(case)
    accepted, stale = _race(
        lambda: _review_correction(case, task),
        lambda: transition_review_task(case.owner, task.pk, action="REVOKE", expected_revision=1),
        lock_table="documents_uploadbatch",
    )
    assert accepted.status == "COMPLETED"
    assert isinstance(stale, RevisionConflict)
    task.refresh_from_db()
    assert task.status == "COMPLETED"
    assert task.revision_number == 2
    transition_review_task(case.owner, task.pk, action="REVOKE", expected_revision=2)
    with pytest.raises(PermissionDenied):
        get_review_task(case.reviewer, task.pk)


@pytest.mark.parametrize("first_writer", ["activation", "review"])
def test_postgresql_reparse_and_review_preserve_revision_lineage(case, first_writer):
    # Missing active-version recheck writes an obsolete task; missing inheritance loses human work.
    if first_writer == "activation":
        revise_observation(case.owner, case.row.pk, action="CORRECT",
                           changes={"raw_value": "6.2"}, expected_revision=0)
    case.row.refresh_from_db()
    task = _started_task(case)
    new = _ready_reparse(case)
    activate = lambda: ParsingVersion.objects.activate(new.parsing_version_id)
    review = lambda: _review_correction(case, task)
    first, second = _race(
        activate if first_writer == "activation" else review,
        review if first_writer == "activation" else activate,
        lock_table="documents_document",
    )

    if first_writer == "activation":
        assert first.pk == new.parsing_version_id
        assert isinstance(second, RevisionConflict)
        task.refresh_from_db()
        assert task.status == "IN_PROGRESS"
        assert task.events.count() == 2
    else:
        assert first.status == "COMPLETED"
        assert second.pk == new.parsing_version_id
    case.row.refresh_from_db()
    new.refresh_from_db()
    effective = effective_observation(new)
    assert case.row.raw_value == "62"
    assert case.row.revisions.count() == 1
    assert new.raw_value == "63"
    assert not new.revisions.exists()
    assert effective.raw_value == "6.2"
    assert effective.revision_conflict is True
    assert "raw_value" in effective.revision_conflicts
    assert effective.original_observation_id == case.row.pk
    assert effective.value_sources["raw_value"]["evidence_id"] == str(case.row.evidence_id)
    assert new.parsing_version.previous_version_id == case.row.parsing_version_id
    assert list(case.document.parsing_versions.filter(active=True).values_list("pk", flat=True)) == [new.parsing_version_id]
    with pytest.raises(RevisionConflict):
        get_review_task(case.reviewer, task.pk)


@pytest.fixture
def publication_case(django_user_model):
    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)
    manager.user_permissions.add(Permission.objects.get(codename="review_labobservation"))
    baseline = current_dictionary()
    definitions = json.loads(current_dictionary().source_path.read_text(encoding="utf-8"))["indicators"]
    cases = []
    for index, (code, alias, unit) in enumerate([
        ("LAB_WBC", "合成并发白细胞", "10^9/L"),
        ("LAB_HGB", "合成并发血红蛋白", "g/L"),
    ]):
        patient = _patient(django_user_model)
        document, row = _observation(patient, date(2026, 8, 20), "5.2", code=f"CANDIDATE_{index}",
                                     raw_name=alias, raw_unit=unit)
        candidate = collect_dictionary_candidates(row.parsing_version)[0]
        task = create_review_task(patient.account, row.pk, reviewer=manager)
        definition = deepcopy(next(item for item in definitions if item["code"] == code))
        definition["aliases"].append(alias)
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition,
                         rationale="Synthetic source checked for concurrency verification", expected_revision=0,
                         totp_verified_at=timezone.now())
        cases.append(SimpleNamespace(patient=patient, document=document, row=row, candidate=candidate,
                                     task=task, code=code, alias=alias, version=f"phase-two-concurrency-{index}"))
    return SimpleNamespace(manager=manager, baseline=baseline, cases=cases)


def _preview(publication_case, item):
    return preview_dictionary(publication_case.manager, candidate_ids=[item.candidate.pk], version=item.version)


def _publish(publication_case, item, preview):
    return publish_dictionary(
        publication_case.manager, version=item.version, candidate_ids=[item.candidate.pk],
        expected_active_hash=preview["expected_active_hash"], expected_preview_hash=preview["preview_hash"],
        totp_verified_at=timezone.now(),
    )


@pytest.mark.parametrize("operation", ["review", "publish", "rollback", "legacy_publish"])
def test_postgresql_dictionary_role_revocation_blocks_waiting_mutation(publication_case, operation):
    from apps.labs.dictionary_workflow import _publication_lock
    from apps.operations.services import publish_dictionary as publish_bundled_dictionary
    from tests.operations.test_services import _withdraw_operator_authority

    item = publication_case.cases[0]
    manager = publication_case.manager
    preview = _preview(publication_case, item) if operation in {"publish", "rollback"} else None
    release = _publish(publication_case, item, preview) if operation == "rollback" else None
    active_hash = current_dictionary().content_hash
    release_ids = set(DictionaryRelease.objects.values_list("pk", flat=True))

    def withdraw():
        with transaction.atomic():
            if operation == "review":
                lock_document_aggregate(item.document.pk)
            else:
                _publication_lock()
            _withdraw_operator_authority(manager, "role")

    def mutate():
        if operation == "review":
            return review_candidate(manager, item.candidate.pk, decision="REJECT", definition={},
                                    rationale="synthetic rejected source", expected_revision=1, totp_verified_at=timezone.now())
        if operation == "publish":
            return _publish(publication_case, item, preview)
        if operation == "rollback":
            return rollback_dictionary(manager, release.previous_release_id, expected_active_hash=active_hash,
                                       totp_verified_at=timezone.now())
        return publish_bundled_dictionary(manager, "v1.0.0.json", reason_code="validated_release",
                                          totp_verified_at=timezone.now())

    _withdrawn, denied = _race(
        withdraw, mutate,
        lock_table="documents_uploadbatch" if operation == "review" else "operations_dictionarypublicationlock",
    )
    assert isinstance(denied, PermissionDenied)
    item.candidate.refresh_from_db()
    assert item.candidate.status == "ACCEPTED"
    assert item.candidate.revision_number == 1
    assert item.candidate.events.count() == 1
    assert current_dictionary().content_hash == active_hash
    assert set(DictionaryRelease.objects.values_list("pk", flat=True)) == release_ids


def test_postgresql_operator_role_revocation_blocks_waiting_activation(case, django_user_model):
    from apps.operations.services import activate_parsing_version
    from tests.operations.test_services import _withdraw_operator_authority

    operator = staff(django_user_model, Role.PROCESSOR_OPERATOR)
    new = _ready_reparse(case)

    def withdraw():
        with transaction.atomic():
            lock_document_aggregate(case.document.pk)
            _withdraw_operator_authority(operator, "role")

    _withdrawn, denied = _race(
        withdraw,
        lambda: activate_parsing_version(operator, new.parsing_version_id, reason_code="validated_rollback",
                                        totp_verified_at=timezone.now()),
        lock_table="documents_uploadbatch",
    )
    assert isinstance(denied, PermissionDenied)
    new.parsing_version.refresh_from_db()
    assert new.parsing_version.active is False
    assert new.parsing_version.status == ParsingVersionStatus.READY
    assert new.parsing_version.processing_run.is_current is False
    assert list(case.document.parsing_versions.filter(active=True).values_list("pk", flat=True)) == [case.row.parsing_version_id]


def test_postgresql_competing_dictionary_publications_accept_only_current_preview(publication_case):
    # Independent patients reach the shared publication guard; no document lock can serialize them.
    left, right = publication_case.cases
    previews = [_preview(publication_case, item) for item in (left, right)]
    accepted, stale = _race(
        lambda: _publish(publication_case, left, previews[0]),
        lambda: _publish(publication_case, right, previews[1]),
        lock_table="operations_dictionarypublicationlock",
    )

    assert accepted.version == left.version
    assert isinstance(stale, RevisionConflict)
    assert DictionaryRelease.objects.count() == 2  # Immutable baseline and one accepted publication.
    assert not DictionaryRelease.objects.filter(version=right.version).exists()
    assert list(DictionaryRelease.objects.filter(active=True).values_list("pk", flat=True)) == [accepted.pk]
    assert current_dictionary().match(left.alias).code == left.code
    assert current_dictionary().match(right.alias) is None
    assert accepted.previous_release.content_hash == publication_case.baseline.content_hash
    assert accepted.regression_report["passed"] is True
    assert accepted.candidate_reviews[0]["candidate_id"] == str(left.candidate.pk)


@pytest.mark.parametrize("first_writer", ["publication", "rollback"])
def test_postgresql_rollback_and_publication_recheck_active_hash_under_shared_lock(publication_case, first_writer):
    # Checking the active hash before the shared lock would overwrite a concurrent publication/rollback.
    left, right = publication_case.cases
    previous = _publish(publication_case, left, _preview(publication_case, left))
    previous_payload = deepcopy(previous.payload)
    preview = _preview(publication_case, right)
    publish = lambda: _publish(publication_case, right, preview)
    rollback = lambda: rollback_dictionary(
        publication_case.manager, previous.previous_release_id,
        expected_active_hash=previous.content_hash, totp_verified_at=timezone.now(),
    )
    accepted, stale = _race(
        publish if first_writer == "publication" else rollback,
        rollback if first_writer == "publication" else publish,
        lock_table="operations_dictionarypublicationlock",
    )

    assert isinstance(stale, RevisionConflict)
    previous.refresh_from_db()
    assert previous.payload == previous_payload
    assert dictionary_for_version(previous.version).match(left.alias).code == left.code
    assert list(DictionaryRelease.objects.filter(active=True).values_list("pk", flat=True)) == [accepted.pk]
    if first_writer == "publication":
        assert accepted.version == right.version
        assert current_dictionary().match(left.alias).code == left.code
        assert current_dictionary().match(right.alias).code == right.code
    else:
        assert accepted.pk == previous.previous_release_id
        assert current_dictionary().content_hash == publication_case.baseline.content_hash
        assert current_dictionary().match(left.alias) is None
        assert current_dictionary().match(right.alias) is None
        assert not DictionaryRelease.objects.filter(version=right.version).exists()


@pytest.mark.parametrize("withdrawal", ["revoke", "delete"])
def test_postgresql_candidate_source_withdrawal_blocks_waiting_publication(publication_case, withdrawal):
    # Candidate evidence authorization must be rechecked after waiting for source aggregate locks.
    item = publication_case.cases[0]
    preview = _preview(publication_case, item)

    def withdraw():
        if withdrawal == "revoke":
            return transition_review_task(item.patient.account, item.task.pk, action="REVOKE", expected_revision=0)
        return request_document_deletion(item.patient, item.document.pk, dispatch=lambda _job: None)

    _withdrawn, denied = _race(
        withdraw, lambda: _publish(publication_case, item, preview), lock_table="documents_uploadbatch",
    )
    assert isinstance(denied, PermissionDenied)
    assert not DictionaryRelease.objects.filter(version=item.version).exists()
    assert current_dictionary().content_hash == publication_case.baseline.content_hash
    with pytest.raises(PermissionDenied):
        get_dictionary_candidate(publication_case.manager, item.candidate.pk)
    if withdrawal == "delete":
        assert not DictionaryCandidate.objects.filter(pk=item.candidate.pk).exists()
