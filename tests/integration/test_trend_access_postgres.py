from concurrent.futures import ThreadPoolExecutor
from datetime import date
from importlib import import_module
from threading import Event

import pytest
from django.db import connection

from apps.patients.access import change_membership
from tests.integration.test_family_postgres_concurrency import thread_call
from tests.labs.test_trends import _observation
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL test database')


@pytest.mark.parametrize('module_name,build_name,path', [
    ('apps.documents.views.records', 'joint_trend_views', '/trends/compare/'),
    ('apps.documents.views.records', 'trend_view', '/trends/LAB_WBC/'),
    ('apps.documents.views.records', 'trend_summaries', '/trends/'),
    ('apps.labs.views', 'comparison_view', '/labs/compare/'),
])
def test_revocation_committed_during_trend_read_stops_its_html_response(django_user_model, monkeypatch, module_name, build_name, path):
    _, patient, client, _, membership = family(django_user_model, 'pg-trend-revoke', 'VIEWER')
    for day, value in ((1, '2'), (2, '4'), (3, '6'), (4, '12')):
        _observation(patient, date(2026, 8, day), value)
    module = import_module(module_name)
    build = getattr(module, build_name)
    entered, resume = Event(), Event()

    def paused(*args, **kwargs):
        result = build(*args, **kwargs)
        entered.set()
        assert resume.wait(timeout=20)
        return result

    monkeypatch.setattr(module, build_name, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: client.get(path, {'patient': str(patient.pk), 'code': 'LAB_WBC'}))
        try:
            assert entered.wait(timeout=20)
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        finally:
            resume.set()
        response = future.result(timeout=20)
    assert response.status_code == 403
    assert 'personal-change' not in response.content.decode()
