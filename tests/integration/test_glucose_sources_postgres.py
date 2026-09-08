"""Actual competing transactions must recheck account, patient and source before import."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
import pytest

from apps.glucose.models import GlucoseRecord
from apps.glucose.services import GlucoseConflict, import_lab_record
from apps.glucose.sources import GlucoseSourceUnavailable, preview_lab
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.glucose.factories import lab_source
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize('change', ['reparse', 'source_revision', 'membership', 'account', 'trash'])
def test_waiting_glucose_import_cannot_outlive_actor_or_source(django_user_model, change):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL glucose database')
    from apps.documents.lifecycle import move_to_trash
    from apps.labs.revisions import revise_observation
    from apps.patients.access import change_membership

    _, patient, _, actor, membership = family(django_user_model, 'glucose-pg-' + change)
    _, _, document, version, observation = lab_source(django_user_model, patient=patient)
    preview = preview_lab(patient, actor, observation.pk)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'reparse':
                lab_source(django_user_model, patient=patient, document=document, previous=version)
            elif change == 'source_revision':
                revise_observation(patient.account, observation.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
            elif change == 'membership':
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            elif change == 'account':
                actor.is_active = False
                actor.save(update_fields=['is_active'])
            else:
                move_to_trash(patient, document.pk, actor=patient.account)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: import_lab_record(patient, actor, observation.pk,
                expected_source=preview['source_fingerprint'], checked_original=True, creation_key=uuid4()), pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        expected = PermissionDenied if change in ('membership', 'account') else (GlucoseConflict, GlucoseSourceUnavailable)
        with pytest.raises(expected):
            future.result(timeout=15)
    assert not GlucoseRecord.objects.filter(patient=patient).exists()
