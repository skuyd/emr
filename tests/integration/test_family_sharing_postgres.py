from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

import pytest
from django.db import connection, transaction

from apps.patients.access import change_membership
from apps.patients.invitations import InvitationUnavailable, accept_invitation, create_invitation, inspect_invitation, revoke_invitation
from apps.patients.models import PatientMembership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _document, _patient
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_invitations import PHONE, recipient
from tests.patients.test_family_shares import create_link, exchange


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


@pytest.mark.parametrize("operation", ["inspect", "accept"])
def test_pending_invitation_waits_for_revocation_then_rejects(django_user_model, operation):
    _, patient = _patient(django_user_model, "pg-inv-revoke")
    _, target = recipient(django_user_model, "pg-inv-target")
    created = create_invitation(patient, patient.account, role="VIEWER", recipient_phone=PHONE)
    function = inspect_invitation if operation == "inspect" else accept_invitation
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revoke_invitation(patient, patient.account, created.invitation.pk)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: function(created.token, target), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(InvitationUnavailable):
            future.result(timeout=15)
    assert not PatientMembership.objects.filter(patient=patient, account=target).exists()


@pytest.mark.parametrize("change", ["creator_downgrade", "recipient_deletion", "already_accepted"])
def test_invitation_acceptance_serializes_with_identity_and_role_changes(django_user_model, change):
    from apps.accounts.deletion import request_account_deletion

    _, patient = _patient(django_user_model, "pg-inv-identity")
    _, own = _patient(django_user_model, "pg-inv-admin")
    creator = PatientMembership.objects.create(patient=patient, account=own.account, role="ADMIN")
    _, target = recipient(django_user_model, "pg-inv-recipient")
    created = create_invitation(patient, own.account, role="VIEWER", recipient_phone=PHONE)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "creator_downgrade":
                change_membership(patient, patient.account, creator.pk, role="EDITOR", expected_revision=0)
            elif change == "recipient_deletion":
                request_account_deletion(target.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
            else:
                accept_invitation(created.token, target)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: accept_invitation(created.token, target), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(InvitationUnavailable):
            future.result(timeout=15)
    assert PatientMembership.objects.filter(patient=patient, account=target).count() == int(change == "already_accepted")


@pytest.mark.parametrize("change", ["revoke", "creator_downgrade", "source_revision", "viewer_deletion"])
def test_share_access_waits_for_committed_lifecycle_change_and_audits_denial(django_user_model, change):
    from apps.accounts.deletion import request_account_deletion
    from apps.facts.revisions import add_manual_fact
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent
    from apps.patients.sharing import revoke_share

    owner, patient = _patient(django_user_model, "pg-share-owner")
    admin, own = _patient(django_user_model, "pg-share-admin")
    member = PatientMembership.objects.create(patient=patient, account=own.account, role="ADMIN")
    reader, reader_own = _patient(django_user_model, "pg-share-reader")
    document, _ = _document(patient)
    token = create_link(admin, patient, document)
    share_id = exchange(reader, token)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "revoke":
                revoke_share(patient, patient.account, share_id)
            elif change == "creator_downgrade":
                change_membership(patient, patient.account, member.pk, role="EDITOR", expected_revision=0)
            elif change == "source_revision":
                add_manual_fact(patient, document.pk, page_number=1, category="DIAGNOSIS", text="合成来源已变", actor=patient.account)
            else:
                request_account_deletion(reader_own.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
                # The viewer is not a member, so deletion does not lock the
                # shared Patient. Exchange must instead wait on Account.
            blocker = backend_pid()
            path = f"/shared/{share_id}/" if change != "viewer_deletion" else "/shared/exchange/"
            if change == "viewer_deletion":
                function = lambda: reader.post(path, {"token": token})
            else:
                function = lambda: reader.get(path)
            future = pool.submit(thread_call, function, pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        response = future.result(timeout=20)
    # Django can replace the denied 401 with SessionInterrupted 400 when
    # account deletion removes a session while middleware saves its lifetime.
    assert response.status_code in ({400, 401, 403, 404, 410} if change == "viewer_deletion" else {401, 403, 404, 410})
    assert AuditEvent.objects.filter(patient_hash=_hash("patient", patient.pk), result="denied",
                                    actor_hash=_hash("actor", reader_own.account_id)).exists()


def test_share_creation_waits_for_source_deletion_and_never_publishes_snapshot(django_user_model):
    from apps.documents.lifecycle import move_to_trash
    from apps.exports.errors import ExportInputError
    from apps.patients.models import PatientShare
    from apps.patients.sharing import create_share

    _, patient = _patient(django_user_model, "pg-share-create")
    document, _ = _document(patient)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            move_to_trash(patient, document.pk, actor=patient.account)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: create_share(patient, patient.account,
                                {"document_ids": [str(document.pk)], "sections": ["sources"]}), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(ExportInputError):
            future.result(timeout=15)
    assert not PatientShare.objects.filter(patient=patient).exists()


def test_existing_read_rechecks_after_revocation_commits_during_render(django_user_model, monkeypatch):
    from apps.documents.views import records
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient = _patient(django_user_model, "pg-final-read-owner")
    reader, own = _patient(django_user_model, "pg-final-read-reader")
    member = PatientMembership.objects.create(patient=patient, account=own.account, role="VIEWER")
    document, _ = _document(patient)
    rendered, release = Event(), Event()
    original = records.render
    def paused_render(*args, **kwargs):
        response = original(*args, **kwargs)
        rendered.set()
        assert release.wait(15)
        return response
    monkeypatch.setattr(records, "render", paused_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: reader.get(f"/records/{document.pk}/"), Queue())
        try:
            assert rendered.wait(10)
            change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
        finally:
            release.set()
        response = future.result(timeout=20)
    assert response.status_code == 403 and document.display_filename not in response.content.decode()
    assert AuditEvent.objects.filter(actor_hash=_hash("actor", own.account_id), action="document_viewed", result="denied").exists()


@pytest.mark.parametrize("page", ["task", "queue"])
def test_professional_review_discards_body_when_grant_revoked_during_render(django_user_model, monkeypatch, page):
    from django.test import Client
    from apps.labs import views
    from apps.labs.review import create_review_task, transition_review_task
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent
    from tests.labs.test_phase_two_comparison import row as make_row
    from tests.labs.test_phase_two_workflows import _reviewer

    _, patient = _patient(django_user_model, f"pg-review-{page}")
    _, observation = make_row(patient)
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, observation.pk, reviewer=reviewer)
    client = Client()
    client.force_login(reviewer)
    rendered, release = Event(), Event()
    original = views._render
    def pause(*args, **kwargs):
        response = original(*args, **kwargs)
        if args[1] != "labs/error.html":
            rendered.set()
            assert release.wait(15)
        return response
    monkeypatch.setattr(views, "_render", pause)
    path = f"/labs/reviews/{task.pk}/" if page == "task" else "/labs/reviews/"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: client.get(path), Queue())
        try:
            assert rendered.wait(10)
            # This is a separate PostgreSQL connection and commits before the
            # HTTP thread can finish its already rendered response.
            transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=0)
        finally:
            release.set()
        response = future.result(timeout=20)
    assert response.status_code == 403 and observation.raw_name not in response.content.decode()
    assert AuditEvent.objects.filter(patient_hash=_hash("patient", patient.pk), actor_hash=_hash("actor", reviewer.pk),
                                     action="review_viewed", result="denied").exists()


def test_share_waits_for_material_review_commit_then_rejects_old_revision(django_user_model):
    from apps.patients.sharing import create_share
    from apps.processing.material_review import review_material
    from tests.documents.test_material_views import material_document

    _, document, version = material_document(django_user_model, "pg-material-share")
    patient = document.patient
    review_material(patient, document.pk, actor=patient.account, action="KEEP_DOCUMENT",
                    expected_version=str(version.pk), expected_revision=0, dispatch=lambda _: None)
    reader, _ = _patient(django_user_model, "pg-material-reader")
    created = create_share(patient, patient.account, {"document_ids": [str(document.pk)], "sections": ["sources"]})
    share_id = exchange(reader, created.token)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            review_material(patient, document.pk, actor=patient.account, action="AUTO",
                            expected_version=str(version.pk), expected_revision=1, dispatch=lambda _: None)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: reader.get(f"/shared/{share_id}/"), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        assert future.result(timeout=20).status_code == 410
