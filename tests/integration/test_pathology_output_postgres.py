"""Normal COMMIT races at the actual selected-output authorization boundaries."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.db import connection, transaction

from apps.accounts.deletion import purge_account_deletion, request_account_deletion
from apps.patients.models import PatientMembership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_pathology_exports import selection
from tests.facts.pathology_factories import add_field, confirm_graph, ihc_fixture, review
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.integration.test_glucose_export_postgres import existing_output


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires an isolated PostgreSQL database")


@pytest.mark.parametrize("target", ["export", "share"])
@pytest.mark.parametrize("change", ["clone", "new_member"])
def test_waiting_selected_output_rejects_committed_unselected_context_change(django_user_model, target, change):
    client, patient, document, report, fields = ihc_fixture(django_user_model, "path-output-pg-" + target + change)
    confirm_graph(patient, fields)
    output, check, expected_error = existing_output(client, patient, selection(document, fields["cps"]), target, django_user_model)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "clone":
                review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CHANGED"}, "raw_value": "SYN-CHANGED"})
            else:
                add_field(patient, report, "assay.method", "assay:a", {"code": "IHC", "raw": "IHC"},
                          {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
            blocker = backend_pid()
            future = pool.submit(thread_call, check, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        # Do not abort the writer: deferred author FKs must actually COMMIT.
        with pytest.raises(expected_error):
            future.result(timeout=20)
    output.refresh_from_db()
    assert output.snapshot == {}
    if target == "export":
        assert output.status == "INVALIDATED"
    else:
        assert output.invalidated_at is not None


@pytest.mark.parametrize("target", ["export", "share"])
def test_actual_http_discards_rendered_body_after_committed_context_author_purge(django_user_model, monkeypatch, target):
    """Author erasure may commit between MVCC reads: test the actual response."""
    from apps.exports import views
    from apps.exports.services import create_preview
    from apps.facts.models import FactRevision
    from apps.patients import share_views
    from apps.patients.sharing import create_share
    from tests.patients.test_family_shares import exchange

    client, patient, document, _, fields = ihc_fixture(django_user_model, "path-author-http-" + target)
    _, collaborator = _patient(django_user_model, "path-output-author-http")
    PatientMembership.objects.create(patient=patient, account=collaborator.account, role="EDITOR")
    review(patient, fields["clone"], actor=collaborator.account)
    confirm_graph(patient, fields)
    author_revision = FactRevision.objects.get(fact=fields["clone"], author=collaborator.account)
    chosen = selection(document, fields["cps"])
    if target == "export":
        assert client.get("/records/").status_code == 200
        output = create_preview(patient, client.session.session_key, chosen, actor=patient.account)
        url, status = f"/visit/{output.pk}/", 409
        render_path, render = "apps.exports.views._render", views._render
    else:
        created = create_share(patient, patient.account, chosen)
        output = created.share
        client, _ = _patient(django_user_model, "path-output-author-recipient")
        identity = exchange(client, created.token)
        url, status = f"/shared/{identity}/", 410
        render_path, render = "apps.patients.share_views.render", share_views.render
    job = request_account_deletion(collaborator.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert client.get(url).status_code == 200
    pids, changed = Queue(), []
    reader_pid = backend_pid()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def during(*args, **kwargs):
            response = render(*args, **kwargs)
            if not changed:
                assert "PD-L1 CPS 21" in response.content.decode()
                changed.append(True)
                result = pool.submit(thread_call, lambda: purge_account_deletion(job.pk), pids).result(timeout=20)
                assert pids.get(timeout=5) != reader_pid
                assert result.outcome == "PURGED"
            return response

        monkeypatch.setattr(render_path, during)
        response = client.get(url)
    assert changed == [True]
    assert response.status_code == status
    assert "CPS 21" not in response.content.decode()
    assert response["Cache-Control"] == "private, no-store, max-age=0"
    author_revision.refresh_from_db()
    assert author_revision.author_id is None
    output.refresh_from_db()
    assert output.snapshot == {}
