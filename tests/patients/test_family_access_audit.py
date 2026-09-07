"""Real HTTP subjects, denied access and streamed outcomes remain auditable."""

import pytest

from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient
from tests.documents.fakes import InMemoryObjectStore
from tests.patients.test_family_shares import create_link, exchange

pytestmark = pytest.mark.django_db


def test_manager_can_filter_actual_readers_and_denied_foreign_resource_access(django_user_model):
    owner, patient = _patient(django_user_model, "audit-owner")
    reader, own = _patient(django_user_model, "audit-reader")
    outsider, foreign = _patient(django_user_model, "audit-outsider")
    PatientMembership.objects.create(patient=patient, account=own.account, role="VIEWER", label="合成阅读者")
    document, _ = _document(patient)
    assert reader.get(f"/records/{document.pk}/").status_code == 200
    assert outsider.get(f"/records/{document.pk}/").status_code == 404
    response = owner.get(f"/patients/{patient.pk}/audit/", {"resource_type": "document", "result": "denied"})
    assert response.status_code == 200
    events = list(response.context["events"])
    assert len(events) == 1
    assert events[0].actor_hash == _hash("actor", foreign.account_id)
    assert events[0].target_hash == _hash("target", document.pk)
    assert events[0].patient_hash == _hash("patient", patient.pk)
    assert events[0].action == "document_viewed" and events[0].result == "denied"
    assert AuditEvent.objects.filter(actor_hash=_hash("actor", own.account_id), action="document_viewed", result="succeeded").exists()
    assert reader.get(f"/patients/{patient.pk}/audit/").status_code == 403
    assert outsider.get(f"/patients/{patient.pk}/audit/").status_code in {403, 404}


def test_mutation_audit_is_patient_scoped_and_contains_no_medical_content(django_user_model):
    from apps.facts.revisions import add_manual_fact

    _, patient = _patient(django_user_model, "audit-fact-owner")
    _, own = _patient(django_user_model, "audit-fact-editor")
    PatientMembership.objects.create(patient=patient, account=own.account, role="EDITOR")
    document, _ = _document(patient)
    fact = add_manual_fact(patient, document.pk, page_number=1, category="DIAGNOSIS", text="不应写入审计的合成医疗内容", actor=own.account)
    event = AuditEvent.objects.get(action="fact_added", target_hash=_hash("target", fact.pk))
    assert event.patient_hash == _hash("patient", patient.pk)
    assert event.actor_hash == _hash("actor", own.account_id) and event.actor_kind == "account"
    assert event.resource_type == "fact"
    assert "合成医疗" not in repr(AuditEvent.objects.values().get(pk=event.pk))


@pytest.mark.parametrize("revoke", [False, True])
def test_stream_has_one_start_and_one_actual_outcome_without_chunk_audit_spam(django_user_model, monkeypatch, revoke):
    from apps.patients.sharing import revoke_share

    owner, patient = _patient(django_user_model, "audit-stream-owner")
    reader, own = _patient(django_user_model, "audit-stream-reader")
    document, _ = _document(patient)
    share_id = exchange(reader, create_link(owner, patient, document, originals=True))
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"x" * 1024 * 1024
    monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
    response = reader.get(f"/shared/{share_id}/documents/{document.pk}/original/")
    iterator = iter(response.streaming_content)
    assert len(next(iterator)) == 256 * 1024
    if revoke:
        revoke_share(patient, patient.account, share_id)
    remaining = b"".join(iterator)
    assert len(remaining) == (0 if revoke else 768 * 1024)
    response.close()
    response.close()
    events = list(AuditEvent.objects.filter(action="original_downloaded", actor_hash=_hash("actor", own.account_id)).order_by("created_at"))
    assert [event.result for event in events] == ["scheduled", "denied" if revoke else "succeeded"]
    assert len({event.request_id for event in events}) == 1


@pytest.mark.parametrize("interruption", ["unstarted", "partial", "read_error"])
def test_interrupted_download_closes_with_exactly_one_terminal_audit(django_user_model, monkeypatch, interruption):
    owner, patient = _patient(django_user_model, f"audit-close-owner-{interruption}")
    reader, own = _patient(django_user_model, f"audit-close-reader-{interruption}")
    document, _ = _document(patient)
    share_id = exchange(reader, create_link(owner, patient, document, originals=True))
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"x" * 1024 * 1024
    monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
    response = reader.get(f"/shared/{share_id}/documents/{document.pk}/original/")
    assert response.status_code == 200 and response.streaming
    downloads = AuditEvent.objects.filter(action="original_downloaded", actor_hash=_hash("actor", own.account_id))
    assert list(downloads.values_list("result", flat=True)) == ["scheduled"]
    if interruption == "partial":
        assert len(next(iter(response.streaming_content))) == 256 * 1024
    elif interruption == "read_error":
        def fail_read(*args):
            raise OSError("synthetic stream read failure")
        monkeypatch.setattr(response._guarded_stream.stream, "read", fail_read)
        with pytest.raises(OSError, match="synthetic stream read failure"):
            next(iter(response.streaming_content))
    # An unstarted generator's close() cannot execute its finally block. The
    # HTTP response itself must finish the audit even before the first byte.
    response.close()
    response.close()
    events = list(downloads)
    # Very short streams may share a clock tick; UUID order is not chronology.
    assert sorted(event.result for event in events) == ["failed", "scheduled"]
    assert len({event.request_id for event in events}) == 1


def test_invitation_and_share_audit_do_not_contain_link_tokens(django_user_model):
    from apps.patients.invitations import create_invitation, accept_invitation

    owner, patient = _patient(django_user_model, "audit-link-owner")
    reader, own = _patient(django_user_model, "audit-link-reader")
    invitation = create_invitation(patient, patient.account, role="VIEWER", recipient_account_id=own.account_id)
    accept_invitation(invitation.token, own.account)
    document, _ = _document(patient)
    token = create_link(owner, patient, document)
    share_id = exchange(reader, token)
    assert reader.get(f"/shared/{share_id}/").status_code == 200
    events = AuditEvent.objects.filter(patient_hash=_hash("patient", patient.pk))
    assert {"invitation_created", "invitation_accepted", "share_created", "share_access_granted", "share_viewed"} <= set(events.values_list("action", flat=True))
    assert token not in repr(list(events.values())) and invitation.token not in repr(list(events.values()))


def test_audit_failure_before_view_still_redacts_link_token(django_user_model, monkeypatch):
    from django.views.debug import ExceptionReporter

    owner, patient = _patient(django_user_model, "audit-failure-owner")
    reader, _ = _patient(django_user_model, "audit-failure-reader")
    document, _ = _document(patient)
    token = create_link(owner, patient, document)
    reader.raise_request_exception = False
    def fail_before_view(*args):
        raise RuntimeError("synthetic audit database failure")
    monkeypatch.setattr("apps.operations.patient_audit._token_subject", fail_before_view)
    response = reader.post("/shared/exchange/", {"token": token})
    assert response.status_code == 500
    reporter = ExceptionReporter(response.wsgi_request, *response.exc_info)
    assert token not in reporter.get_traceback_text()


def test_existing_read_discards_rendered_body_when_member_is_revoked_django_boundary(django_user_model, monkeypatch):
    from apps.patients.access import change_membership
    from apps.documents.views import records

    _, patient = _patient(django_user_model, "read-boundary-owner")
    reader, own = _patient(django_user_model, "read-boundary-reader")
    member = PatientMembership.objects.create(patient=patient, account=own.account, role="VIEWER")
    document, _ = _document(patient)
    original_render = records.render
    def render_then_revoke(*args, **kwargs):
        response = original_render(*args, **kwargs)
        change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
        return response
    monkeypatch.setattr(records, "render", render_then_revoke)
    response = reader.get(f"/records/{document.pk}/")
    assert response.status_code == 403
    assert document.display_filename not in response.content.decode()
    assert AuditEvent.objects.filter(actor_hash=_hash("actor", own.account_id), action="document_viewed", result="denied").exists()


@pytest.mark.parametrize("kind", ["share", "invitation"])
def test_creation_render_failure_redacts_token_from_deep_library_context(django_user_model, monkeypatch, kind):
    from django.views.debug import get_exception_reporter_class
    from django.views.decorators.debug import sensitive_variables
    from apps.patients.tokens import create_token

    owner, patient = _patient(django_user_model, f"render-token-owner-{kind}")
    document, _ = _document(patient)
    observed = []
    @sensitive_variables()
    def capture(purpose):
        value = create_token(purpose)
        observed.append(value[0])
        return value
    def broken_render(request, template, context, **kwargs):
        raise RuntimeError("synthetic template failure")
    module = "sharing" if kind == "share" else "invitations"
    view_module = "share_views" if kind == "share" else "invitation_views"
    monkeypatch.setattr(f"apps.patients.{module}.create_token", capture)
    monkeypatch.setattr(f"apps.patients.{view_module}.render", broken_render)
    data = {"document_ids": [str(document.pk)], "sections": ["patient"]} if kind == "share" else {"recipient_phone": "+8613900000031", "role": "VIEWER"}
    owner.raise_request_exception = False
    response = owner.post(f"/patients/{patient.pk}/{kind}s/", data)
    assert response.status_code == 500 and len(observed) == 1
    reporter = get_exception_reporter_class(response.wsgi_request)(response.wsgi_request, *response.exc_info)
    assert observed[0] not in reporter.get_traceback_text()
    assert observed[0] not in reporter.get_traceback_html()
