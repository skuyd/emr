"""Normal independent COMMITs must fence both notices and navigation responses."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

from django.db import connection, transaction
import pytest

from apps.cloud_imaging import views
from apps.cloud_imaging.services import open_source
from apps.patients.access import change_membership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cloud_imaging.test_controlled_open import payload
from tests.cloud_imaging.test_source_services import SECOND_URL, _decide
from tests.integration.test_cloud_sources_postgres import case, manual
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the dedicated cloud controlled-open PostgreSQL database')


def purge(actor):
    from apps.accounts.deletion import AccountDeletionOutcome, request_account_deletion, purge_account_deletion
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED


@pytest.mark.parametrize('phase', ['notice', 'redirect', 'scripted'])
@pytest.mark.parametrize('change', ['revision', 'revoke', 'author_purge', 'trash'])
def test_committed_change_after_response_build_never_delivers_previous_target(django_user_model, monkeypatch, phase, change):
    from apps.documents.lifecycle import move_to_trash

    owner, patient, member, actor, membership, document, page, _ = case(django_user_model, phase + change)
    source = _decide(patient, manual(patient, actor, document, page), 'CONFIRM')
    client = owner if change == 'author_purge' else member
    data = payload(patient, source)
    entered, resume = Event(), Event()
    seam = '_render' if phase == 'notice' else '_external_redirect'
    real = getattr(views, seam)
    def paused(*args, **kwargs):
        response = real(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert resume.wait(20)
        return response
    monkeypatch.setattr(views, seam, paused)
    action = (lambda: client.get(f'/cloud-imaging/{source.pk}/visit/')) if phase == 'notice' else (
        lambda: client.post(f'/cloud-imaging/{source.pk}/open/', data,
                            **({'HTTP_X_CLOUD_OPEN': 'navigate'} if phase == 'scripted' else {})))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(thread_call, action)
        try:
            assert entered.wait(20)
            assert not connection.in_atomic_block
            if change == 'revision':
                _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
            elif change == 'revoke':
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            elif change == 'author_purge':
                purge(actor)
                source.refresh_from_db()
                assert source.created_by_id is None and source.revisions.first().author_id is None
            else:
                move_to_trash(patient, document.pk, actor=patient.account)
            assert not connection.in_atomic_block
        finally:
            resume.set()
        response = pending.result(timeout=20)
    assert response.status_code == (404 if change in {'revoke', 'trash'} else 409)
    assert 'Location' not in response
    assert b'SYNTHETIC_FIRST' not in response.content and b'SYNTHETIC_CORRECTION' not in response.content
    assert response['Referrer-Policy'] == 'no-referrer' and 'no-store' in response['Cache-Control']


@pytest.mark.parametrize('change', ['revision', 'revoke'])
def test_open_service_waits_for_actual_uncommitted_change_before_resolving(django_user_model, change):
    from django.core.exceptions import PermissionDenied
    from apps.cloud_imaging.services import CloudConflict

    _, patient, _, actor, membership, document, page, _ = case(django_user_model, 'locked-' + change)
    source = _decide(patient, manual(patient, actor, document, page), 'CONFIRM')
    data = payload(patient, source)
    data.pop('patient_id')
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'revision':
                _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
            else:
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            pending = pool.submit(thread_call,
                lambda: open_source(patient, actor=actor, source_id=source.pk, **data), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(CloudConflict if change == 'revision' else PermissionDenied):
            pending.result(timeout=20)


@pytest.mark.parametrize('invalid_post', [False, True])
def test_report_exclusion_commits_during_notice_or_invalid_form_validation(django_user_model, monkeypatch, invalid_post):
    from apps.cloud_imaging.forms import OpenForm
    from tests.cloud_imaging.test_error_form_freshness import exclude_report, invalid_request

    client, patient, document, report, _, _, _ = invalid_request(django_user_model, 'source')
    source = document.cloud_imaging_sources.get()
    source = _decide(patient, source, 'REASSIGN', changes={'report_id': str(report.pk)})
    data = {**payload(patient, source), 'expected_revision': 'invalid'}
    entered, resume = Event(), Event()
    target, attribute = (OpenForm, 'is_valid') if invalid_post else (views, '_render')
    original = getattr(target, attribute)
    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert resume.wait(20)
        return result
    monkeypatch.setattr(target, attribute, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(thread_call, lambda: client.post(f'/cloud-imaging/{source.pk}/open/', data)
            if invalid_post else client.get(f'/cloud-imaging/{source.pk}/visit/'))
        try:
            assert entered.wait(20)
            exclude_report(patient, report)
            assert not connection.in_atomic_block
        finally:
            resume.set()
        response = pending.result(timeout=20)
    assert response.status_code == 409 and 'Location' not in response
    assert 'expected_source' not in response.content.decode()
