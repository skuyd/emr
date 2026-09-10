from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

from django.db import connection
import pytest

from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.access import change_membership
from tests.cancer_ordering.test_lab_ordering import labs
from tests.cancer_ordering.test_services import _collect, _select
from tests.facts.factories import parsed_facts
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated cancer-ordering PostgreSQL database')


@pytest.mark.parametrize('path', ['/labs/compare/', '/trends/', '/trends/compare/'])
@pytest.mark.parametrize('change', ['selection', 'parent', 'new_input', 'membership'])
def test_committed_ordering_dependency_change_rejects_rendered_callers(django_user_model, monkeypatch, path, change):
    from apps.documents.views import records
    from apps.labs import views

    _, patient, member, _, membership = family(django_user_model, 'cancer-pg-callers-' + path + change)
    labs(patient)
    _, version = _collect(patient)
    initial = member.get(path)
    assert initial.status_code == 200 and '肺癌指标顺序' in initial.content.decode()
    initial.close()
    entered, release, pids = Event(), Event(), Queue()
    module = views if path.startswith('/labs/') else records
    original = module.render

    def pause_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=30)
        return response

    monkeypatch.setattr(module, 'render', pause_after_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: member.get(path), pids)
        request_pid = pids.get(timeout=10)
        try:
            assert entered.wait(timeout=20) and request_pid != backend_pid()
            assert not connection.in_atomic_block
            if change == 'selection':
                _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
            elif change == 'parent':
                fact = Fact.objects.get(parsing_version=version)
                revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
            elif change == 'new_input':
                parsed_facts(patient, ['出院诊断：胰腺癌。'])
            else:
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            # The independent connection has committed normally before the
            # original rendered response is released; no verdict is mocked.
            assert not connection.in_atomic_block
        finally:
            release.set()
        response = future.result(timeout=30)
    assert response.status_code == (403 if change == 'membership' else 409)
    body = response.content.decode()
    assert 'LAB_' not in body and '白细胞' not in body and '肺癌指标顺序' not in body
    response.close()
