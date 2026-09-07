"""Invitations bind the chosen recipient without becoming a role escalation path."""

from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone

from apps.accounts.crypto import hash_phone
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db
PHONE = "+8613900000021"


class InvitationLink(HTMLParser):
    value = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "invitation_link":
            self.value = attrs.get("value", "")


def recipient(model, marker):
    client, own = _patient(model, marker)
    model.objects.filter(pk=own.account_id).update(phone_hash=hash_phone(PHONE))
    own.account.refresh_from_db()
    return client, own.account


def invite(client, patient, role="VIEWER", phone=PHONE):
    response = client.post(f"/patients/{patient.pk}/invitations/", {
        "recipient_phone": phone, "role": role, "label": "家人",
    })
    assert response.status_code == 201
    parser = InvitationLink()
    parser.feed(response.content.decode())
    link = urlsplit(parser.value)
    assert link.path == "/family/invitation/" and not link.query
    return parse_qs(link.fragment)["token"][0]


def test_invited_recipient_can_accept_read_only_and_cannot_replay(django_user_model):
    owner, patient = _patient(django_user_model, "invitation-owner")
    reader, account = recipient(django_user_model, "invitation-recipient")
    document, _ = _document(patient)
    token = invite(owner, patient)
    accepted = reader.post("/family/invitation/accept/", {"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["patient_id"] == str(patient.pk)
    assert reader.get(f"/records/{document.pk}/").status_code == 200
    assert reader.post(f"/records/{document.pk}/delete/", {"patient_id": str(patient.pk), "confirmation": "delete"}).status_code == 403
    assert reader.post("/family/invitation/accept/", {"token": token}).status_code == 410
    assert PatientMembership.objects.get(patient=patient, account=account).role == "VIEWER"


@pytest.mark.parametrize("actor_role,invited_role,expected", [
    ("OWNER", "ADMIN", 201), ("ADMIN", "EDITOR", 201), ("ADMIN", "ADMIN", 403),
    ("EDITOR", "VIEWER", 403), ("VIEWER", "VIEWER", 403),
])
def test_invitation_role_ceiling_is_enforced_by_the_endpoint(django_user_model, actor_role, invited_role, expected):
    owner, patient = _patient(django_user_model, "invite-role-owner")
    actor = owner
    if actor_role != "OWNER":
        actor, own = _patient(django_user_model, "invite-role-actor")
        PatientMembership.objects.create(patient=patient, account=own.account, role=actor_role)
    response = actor.post(f"/patients/{patient.pk}/invitations/", {"recipient_phone": PHONE, "role": invited_role})
    assert response.status_code == expected


def test_other_account_cannot_inspect_or_accept_phone_bound_invitation(django_user_model):
    owner, patient = _patient(django_user_model, "invite-binding-owner")
    correct, account = recipient(django_user_model, "invite-binding-target")
    outsider, other = _patient(django_user_model, "invite-binding-outsider")
    token = invite(owner, patient)
    assert outsider.post("/family/invitation/inspect/", {"token": token}).status_code == 410
    assert outsider.post("/family/invitation/accept/", {"token": token}).status_code == 410
    assert not PatientMembership.objects.filter(patient=patient, account=other.account).exists()
    assert correct.post("/family/invitation/accept/", {"token": token}).status_code == 200


def test_existing_member_invitation_is_consumed_without_upgrading_role(django_user_model):
    owner, patient = _patient(django_user_model, "invite-existing-owner")
    reader, account = recipient(django_user_model, "invite-existing-target")
    membership = PatientMembership.objects.create(patient=patient, account=account, role="VIEWER")
    token = invite(owner, patient, role="ADMIN")
    assert reader.post("/family/invitation/accept/", {"token": token}).status_code == 409
    membership.refresh_from_db()
    assert membership.role == "VIEWER" and membership.revision == 0
    assert reader.post("/family/invitation/accept/", {"token": token}).status_code == 410


@pytest.mark.parametrize("invalidation", ["expired", "revoked", "creator_downgraded"])
def test_pending_invitation_rechecks_expiry_revocation_and_creator(django_user_model, invalidation):
    from apps.patients.models import PatientInvitation

    owner, patient = _patient(django_user_model, "invite-stale-owner")
    administrator, own = _patient(django_user_model, "invite-stale-admin")
    member = PatientMembership.objects.create(patient=patient, account=own.account, role="ADMIN")
    reader, account = recipient(django_user_model, "invite-stale-target")
    token = invite(administrator, patient)
    invitation = PatientInvitation.objects.get(patient=patient)
    if invalidation == "expired":
        PatientInvitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    elif invalidation == "revoked":
        assert owner.post(f"/patients/{patient.pk}/invitations/{invitation.pk}/revoke/").status_code == 302
    else:
        change_membership(patient, patient.account, member.pk, role="EDITOR", expected_revision=0)
    assert reader.post("/family/invitation/accept/", {"token": token}).status_code == 410
    assert not PatientMembership.objects.filter(patient=patient, account=account).exists()


def test_restoring_revoked_member_requires_new_invitation_and_new_revision(django_user_model):
    owner, patient = _patient(django_user_model, "invite-restore-owner")
    reader, account = recipient(django_user_model, "invite-restore-target")
    member = PatientMembership.objects.create(patient=patient, account=account, role="EDITOR")
    change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
    token = invite(owner, patient)
    assert reader.post("/family/invitation/accept/", {"token": token}).status_code == 200
    member.refresh_from_db()
    assert member.revoked_at is None and member.revision == 2 and member.role == "VIEWER"


def test_invitation_persists_only_token_and_recipient_digests(django_user_model):
    from apps.patients.models import PatientInvitation

    owner, patient = _patient(django_user_model, "invite-storage-owner")
    token = invite(owner, patient)
    invitation = PatientInvitation.objects.get(patient=patient)
    stored = repr(PatientInvitation.objects.values().get(pk=invitation.pk))
    assert token not in stored and PHONE not in stored
    assert invitation.recipient_phone_hash == hash_phone(PHONE)
    assert 6 * 86400 < (invitation.expires_at - invitation.created_at).total_seconds() <= 7 * 86400
    assert token not in repr(dict(owner.session))


def test_invitation_post_requires_csrf_and_uses_fixed_login_return(django_user_model):
    from django.test import Client

    owner, patient = _patient(django_user_model, "invite-csrf-owner")
    token = invite(owner, patient)
    anonymous = Client(enforce_csrf_checks=True)
    anonymous.get("/family/invitation/")
    assert anonymous.post("/family/invitation/accept/", {"token": token}).status_code == 403
    response = anonymous.post("/family/invitation/accept/", {"token": token}, HTTP_X_CSRFTOKEN=anonymous.cookies["csrftoken"].value)
    assert response.status_code == 401
    login = urlsplit(response.json()["login_url"])
    assert parse_qs(login.query) == {"next": ["/family/invitation/"]}
    assert token not in repr(dict(anonymous.session)) and token not in response.content.decode()


def test_account_bound_service_invitation_does_not_accept_another_phone(django_user_model):
    from apps.patients.invitations import create_invitation

    _, patient = _patient(django_user_model, "invite-uuid-owner")
    reader, account = recipient(django_user_model, "invite-uuid-reader")
    other, _ = _patient(django_user_model, "invite-uuid-other")
    created = create_invitation(patient, patient.account, role="VIEWER", recipient_account_id=account.pk)
    assert other.post("/family/invitation/accept/", {"token": created.token}).status_code == 410
    assert reader.post("/family/invitation/accept/", {"token": created.token}).status_code == 200


def test_invitation_error_reporting_redacts_token(django_user_model, monkeypatch, settings):
    from django.views.debug import ExceptionReporter
    from django.views.decorators.debug import sensitive_variables
    from apps.patients import invitation_views

    settings.DEBUG = False
    owner, patient = _patient(django_user_model, "invite-errors-owner")
    token = invite(owner, patient)
    @sensitive_variables()
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic database failure")
    monkeypatch.setattr(invitation_views, "inspect_invitation", fail)
    owner.raise_request_exception = False
    response = owner.post("/family/invitation/inspect/", {"token": token})
    assert response.status_code == 500
    report = ExceptionReporter(response.wsgi_request, *response.exc_info).get_traceback_text()
    assert token not in report
