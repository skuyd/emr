from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

from django.db import connection
import pytest

from apps.cancer_ordering.models import CandidateRevision, SelectionRevision
from apps.cancer_ordering.services import revise_candidate
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.access import change_membership
from tests.cancer_ordering.test_services import _collect, _row
from tests.cancer_ordering.test_views import INDEX, _choice, _decision, _url
from tests.facts.factories import parsed_facts
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated cancer-ordering PostgreSQL test database')


@pytest.mark.parametrize('change, page', [
    ('parent', 'detail'), ('parent', 'invalid_candidate'), ('new_input', 'invalid_choice'),
    ('membership', 'invalid_choice'), ('author', 'index'),
])
def test_committed_change_after_render_discards_body_and_bound_choices(django_user_model, monkeypatch, change, page):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.cancer_ordering import views

    owner, patient, member, actor, membership = family(django_user_model, 'cancer-pg-ui-' + change + page)
    _, version = _collect(patient)
    row = _row(patient)
    client = member
    if change == 'author':
        revise_candidate(patient, row['id'], actor=actor, action='CONFIRM', expected_revision=0,
                         expected_source=row['current_source_token'], checked_original=True)
        client = owner
    initial_count = CandidateRevision.objects.count()
    post = _decision(patient, row, checked_original='') if page == 'invalid_candidate' else _choice(patient, mode='BAD')
    path = _url(row) if page in {'detail', 'invalid_candidate'} else INDEX
    entered, release, pids = Event(), Event(), Queue()
    original_render = views.render
    def pause_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=30)
        return response
    monkeypatch.setattr(views, 'render', pause_after_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call,
            lambda: client.post(path, post) if page.startswith('invalid') else client.get(path), pids)
        request_pid = pids.get(timeout=10)
        try:
            assert entered.wait(timeout=20) and request_pid != backend_pid()
            assert not connection.in_atomic_block
            if change == 'parent':
                fact = Fact.objects.get(parsing_version=version)
                revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
            elif change == 'new_input':
                parsed_facts(patient, ['出院诊断：胰腺癌。'])
            elif change == 'membership':
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            else:
                job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
                assert purge_account_deletion(job.pk).outcome == 'PURGED'
            assert not connection.in_atomic_block
        finally:
            release.set()
        response = future.result(timeout=30)
    assert response.status_code == (403 if change == 'membership' else 409)
    body = response.content.decode()
    assert 'name="expected_revision"' not in body and '出院诊断' not in body
    assert CandidateRevision.objects.count() == initial_count and not SelectionRevision.objects.exists()
    response.close()


@pytest.mark.parametrize('detail', [False, True])
def test_actual_document_purge_during_initial_source_read_returns_retry(django_user_model, monkeypatch, detail):
    from apps.cancer_ordering.sources import SourceContext
    from apps.documents.deletion import request_document_deletion, purge_document_deletion
    from tests.documents.fakes import InMemoryObjectStore

    owner, patient, _, _, _ = family(django_user_model, 'cancer-pg-ui-initial-purge-' + str(detail))
    document, _ = _collect(patient)
    row = _row(patient)
    entered, release, pids = Event(), Event(), Queue()
    original_fact = SourceContext.fact
    def pause_before_fact(self, identity):
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        return original_fact(self, identity)
    monkeypatch.setattr(SourceContext, 'fact', pause_before_fact)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: owner.get(_url(row) if detail else INDEX), pids)
        request_pid = pids.get(timeout=10)
        try:
            assert entered.wait(timeout=20) and request_pid != backend_pid()
            job = request_document_deletion(patient, document.pk, actor=patient.account, dispatch=lambda _: None)
            assert purge_document_deletion(job.pk, InMemoryObjectStore()).outcome == 'PURGED'
            assert not connection.in_atomic_block
        finally:
            release.set()
        response = future.result(timeout=30)
    assert response.status_code == 409 and '出院诊断' not in response.content.decode()
    response.close()
