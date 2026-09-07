"""Real lock waits, revocation and source updates in an isolated PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.facts.revisions import revise_fact
from apps.patients.access import authorize_patient, change_membership
from apps.treatments.derivations import decide_proposal, persist_proposals, proposal_preview
from apps.treatments.models import TreatmentCycle, TreatmentDerivationRun, TreatmentRevision
from apps.treatments.services import TreatmentConflict
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family
from tests.treatments.test_derivation_service import fact

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def test_identical_cycle_decision_really_waits_and_commits_once(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-replay")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    proposal = preview["proposals"]["cycles"][0]
    arguments = dict(actor=actor, expected_fingerprint=preview["input_fingerprint"], action="CONFIRM",
                     expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = decide_proposal(patient, proposal["id"], **arguments)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: decide_proposal(patient, proposal["id"], **arguments), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        second = future.result(timeout=15)
    assert first.pk == second.pk
    assert TreatmentRevision.objects.filter(cycle=first.cycle).count() == 1
    assert TreatmentDerivationRun.objects.count() == 1


def test_source_revision_fences_proposal_waiting_on_the_patient_guard(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-source")
    original = fact(patient)
    preview = proposal_preview(patient, actor=actor)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_fact(patient, original.pk, actor=patient.account, action="CORRECT", expected_revision=0,
                        changes={"text": "2024-04-01给予方案乙化疗。"}, checked_original=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: persist_proposals(patient, actor=actor,
                expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(TreatmentConflict):
            future.result(timeout=15)
    assert not TreatmentDerivationRun.objects.exists() and not TreatmentCycle.objects.exists()


@pytest.mark.parametrize("change", ["parse", "trash_restore"])
def test_source_lifecycle_change_fences_a_waiting_cycle_confirmation(django_user_model, change):
    from apps.documents.lifecycle import move_to_trash, restore_document
    from apps.processing.models import ParsingVersion
    from tests.facts.factories import parsed_facts
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-source-lifecycle-" + change)
    document, first = parsed_facts(patient, ["治疗经过：", "2024-03-01给予方案甲C1D1化疗。"])
    _, second = parsed_facts(patient, ["治疗经过：", "2024-03-02给予方案乙C1D1化疗。"], document=document, previous=first)
    ParsingVersion.objects.activate(first)
    preview = proposal_preview(patient, actor=actor)
    proposal = preview["proposals"]["cycles"][0]
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "parse":
                ParsingVersion.objects.activate(second)
            else:
                move_to_trash(patient, document.pk, actor=actor)
                restore_document(patient, document.pk, actor=actor)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: decide_proposal(patient, proposal["id"], actor=actor,
                expected_fingerprint=preview["input_fingerprint"], action="CONFIRM", expected_revision=0,
                checked_original=True, operation_id=uuid.uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(TreatmentConflict):
            future.result(timeout=15)
    assert not TreatmentCycle.objects.exists() and not TreatmentRevision.objects.exists()


def test_revoked_editor_cannot_save_a_waiting_derivation(django_user_model):
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-revoke")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: persist_proposals(patient, actor=actor,
                expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
    assert not TreatmentDerivationRun.objects.exists()


@pytest.mark.parametrize("account_delete", [False, True])
def test_pending_revocation_and_account_deletion_do_not_deadlock_real_author_foreign_keys(django_user_model, account_delete):
    from apps.accounts.deletion import request_account_deletion
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-author-delete" if account_delete else "pg-treat-author-revoke")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    proposal = preview["proposals"]["cycles"][0]
    def revoke():
        if account_delete:
            return request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        return change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, actor, "write", lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, revoke, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
            decision = decide_proposal(patient, proposal["id"], actor=actor, expected_fingerprint=preview["input_fingerprint"],
                action="CONFIRM", expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
        future.result(timeout=15)
    assert decision.author_id == actor.pk and decision.cycle.created_by_id == actor.pk
    assert TreatmentDerivationRun.objects.get().requested_by_id == actor.pk
    with pytest.raises(PermissionDenied):
        proposal_preview(patient, actor=actor)


def test_read_preview_revalidates_actor_after_consistent_material_build(django_user_model, monkeypatch):
    from apps.treatments import derivations
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-read-revoke")
    fact(patient)
    entered, release = Event(), Event()
    real = derivations.authorize_patient
    def paused(*args, **kwargs):
        if not kwargs.get("lock"):
            entered.set()
            assert release.wait(timeout=15)
        return real(*args, **kwargs)
    monkeypatch.setattr(derivations, "authorize_patient", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: derivations.proposal_preview(patient, actor=actor))
        try:
            assert entered.wait(timeout=10)
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        finally:
            release.set()
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)


def test_workspace_archival_and_proposal_reads_share_one_patient_guard(django_user_model, monkeypatch):
    from apps.documents import archive
    from apps.treatments.services import revise_event
    from tests.treatments.test_manual_events import create
    owner, patient, _, actor, _ = family(django_user_model, "pg-treat-coherent-workspace")
    event = create(patient, actor)
    entered, release = Event(), Event()
    reader_pids, writer_pids = Queue(), Queue()
    original = archive.records_context
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=15)
        return original(*args, **kwargs)
    monkeypatch.setattr(archive, "records_context", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(thread_call, lambda: owner.get("/treatments/", {"patient": str(patient.pk)}), reader_pids)
        try:
            reader = reader_pids.get(timeout=10)
            assert entered.wait(timeout=10)
            write = pool.submit(thread_call, lambda: revise_event(patient, event.pk, actor=actor, action="CORRECT", expected_revision=1,
                operation_id=uuid.uuid4(), checked_original=True, changes={"title": "并发更新后的记录"}), writer_pids)
            wait_until_backend_is_blocked_by(state, request_pid=writer_pids.get(timeout=10), blocker_pid=reader)
        finally:
            release.set()
        response = read.result(timeout=15)
        write.result(timeout=15)
    assert response.status_code == 200 and "并发更新后的记录" not in response.content.decode()
    assert "并发更新后的记录" in owner.get("/treatments/").content.decode()


@pytest.mark.parametrize("decision", ["merge", "split"])
def test_merge_or_split_replay_really_waits_and_creates_one_lineage(django_user_model, decision):
    from apps.treatments.cycles import merge_cycles, split_cycle
    from apps.treatments.models import CycleLineage
    from tests.treatments.test_cycle_decisions import cycle
    from tests.treatments.test_manual_events import create
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-lineage-" + decision)
    one, two = create(patient, actor), create(patient, actor, occurred_on="2024-03-21")
    operation = uuid.uuid4()
    if decision == "merge":
        left, right = cycle(patient, [one]), cycle(patient, [two], anchor="2024-03-21")
        def mutate():
            return [merge_cycles(patient, actor=actor, cycle_ids=[left.pk, right.pk],
                expected_revisions={str(left.pk): 1, str(right.pk): 1},
                resolution={"anchor": None, "anchor_precision": "UNKNOWN"}, checked_original=True, operation_id=operation)]
    else:
        parent = cycle(patient, [one, two])
        def mutate():
            return split_cycle(patient, parent.pk, actor=actor, expected_revision=1,
                parts=[{"event_ids": [str(one.pk)], "anchor": "2024-02-29", "anchor_precision": "DAY", "ordinal": None},
                       {"event_ids": [str(two.pk)], "anchor": "2024-03-21", "anchor_precision": "DAY", "ordinal": None}],
                checked_original=True, operation_id=operation)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = mutate()
            future = pool.submit(thread_call, mutate, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=backend_pid())
        second = future.result(timeout=15)
    assert {row.pk for row in first} == {row.pk for row in second}
    assert TreatmentCycle.objects.count() == 3 and CycleLineage.objects.count() == 2
    assert TreatmentRevision.objects.filter(operation_id=operation).count() == 3
    assert set(TreatmentRevision.objects.filter(operation_id=operation).values_list("author_id", flat=True)) == {actor.pk}


def test_competing_record_assignments_wait_then_reject_the_stale_assignment_token(django_user_model):
    from apps.treatments.records import assign_record, record_state
    from apps.treatments.models import CycleRecordLink
    from tests.documents.test_detail_viewer import _document
    from tests.treatments.test_cycle_decisions import cycle
    from tests.treatments.test_manual_events import create
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-record-competition")
    document, _ = _document(patient)
    left, right = cycle(patient, [create(patient, actor)]), cycle(patient, [create(patient, actor)])
    old = record_state(patient, actor=actor, kind="document", identity=document.pk)
    arguments = dict(actor=actor, kind="document", identity=document.pk, expected_source=old["source_token"],
        expected_assignment=old["assignment_token"], expected_revision=1, checked_original=True, assigned=True)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = assign_record(patient, left.pk, operation_id=uuid.uuid4(), **arguments)
            future = pool.submit(thread_call, lambda: assign_record(patient, right.pk, operation_id=uuid.uuid4(), **arguments), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=backend_pid())
        with pytest.raises(TreatmentConflict):
            future.result(timeout=15)
    assert first.author_id == actor.pk
    assert list(CycleRecordLink.objects.filter(document=document, active=True).values_list("cycle_id", flat=True)) == [left.pk]
    assert not TreatmentRevision.objects.filter(cycle=right, action="ASSIGN_RECORD").exists()


@pytest.mark.parametrize("change", ["event", "membership", "source"])
def test_derived_export_build_rechecks_independently_committed_changes_before_publication(django_user_model, monkeypatch, change):
    from apps.exports import services
    from apps.treatments.services import revise_event
    from tests.documents.fakes import InMemoryObjectStore
    from tests.exports.test_treatment_exports import selection
    from tests.treatments.test_manual_events import create
    _, patient, client, actor, membership = family(django_user_model, "pg-treat-export-build-" + change)
    original = fact(patient)
    event = create(patient, actor)
    job = services.create_preview(patient, client.session.session_key, selection(treatment_event_ids=[str(event.pk)]), actor=actor)
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None, actor=actor)
    entered, release = Event(), Event()
    build = services.build_artifact
    streams = []
    def paused_build(*args, **kwargs):
        artifact = build(*args, **kwargs)
        streams.append(artifact.stream)
        entered.set()
        assert release.wait(timeout=15)
        return artifact
    monkeypatch.setattr(services, "build_artifact", paused_build)
    store = InMemoryObjectStore()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: services.generate_export(job.pk, store))
        try:
            assert entered.wait(timeout=10)
            if change == "event":
                revise_event(patient, event.pk, actor=actor, action="CORRECT", expected_revision=1,
                    changes={"title": "已更新的合成治疗记录"}, checked_original=True, operation_id=uuid.uuid4())
            elif change == "membership":
                change_membership(patient, patient.account, membership.pk, role="VIEWER", expected_revision=0)
            else:
                # Even a source outside the displayed selection may change the
                # full proposal/organization context. The snapshot binds it.
                revise_fact(patient, original.pk, actor=actor, action="CORRECT", expected_revision=0,
                    changes={"text": "治疗经过：2024-04-01给予方案乙化疗。"}, checked_original=True)
        finally:
            release.set()
        future.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {} and store.objects == {}
    assert streams and all(stream.closed for stream in streams)


@pytest.mark.parametrize("change", ["cycle", "source"])
def test_derived_http_stream_stops_after_a_committed_cycle_or_source_change(django_user_model, monkeypatch, change):
    from apps.exports import services, views
    from apps.treatments.cycles import revise_cycle
    from tests.documents.fakes import InMemoryObjectStore
    from tests.exports.test_treatment_exports import selection
    from tests.treatments.test_cycle_decisions import cycle
    from tests.treatments.test_manual_events import create
    _, patient, client, actor, _ = family(django_user_model, "pg-treat-stream-" + change)
    original = fact(patient)
    item = cycle(patient, [create(patient, actor)])
    job = services.create_preview(patient, client.session.session_key, selection(cycle_ids=[str(item.pk)]), actor=actor)
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None, actor=actor)
    store = InMemoryObjectStore()
    services.generate_export(job.pk, store)
    monkeypatch.setattr(views, "get_object_store", lambda: store)
    response = client.get(f"/visit/{job.pk}/download/", {"patient": str(patient.pk)})
    assert response.status_code == 200
    response.block_size = 256
    stream = iter(response.streaming_content)
    assert len(next(stream)) == 256
    def mutate():
        if change == "cycle":
            return revise_cycle(patient, item.pk, actor=actor, action="REVOKE", expected_revision=1, operation_id=uuid.uuid4())
        return revise_fact(patient, original.pk, actor=actor, action="CORRECT", expected_revision=0,
            changes={"text": "治疗经过：2024-04-01给予方案乙化疗。"}, checked_original=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(thread_call, mutate).result(timeout=15)
    # HTTP streaming middleware terminates a denied stream after recording its
    # terminal audit; no exception text or additional bytes enter the response.
    assert b"".join(stream) == b""
    response.close()
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {}
    from apps.operations.models import AuditEvent
    assert AuditEvent.objects.filter(action="export_downloaded", result="denied").count() == 1
