"""Recipient-bound invitations serialize acceptance with revocation and deletion."""

from dataclasses import dataclass, field
from datetime import timedelta
import hmac
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.accounts.crypto import hash_phone
from apps.accounts.models import Account
from apps.accounts.phone import normalize_mainland_phone
from apps.operations.audit import record_audit_event
from .access import Capability, authorize_patient
from .models import PatientInvitation, PatientMembership
from .tokens import create_token, token_digest


class InvitationUnavailable(ValueError):
    def __init__(self):
        super().__init__("邀请已失效或不属于当前账号，请联系邀请人重新生成。")


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: PatientInvitation
    token: str = field(repr=False)


@dataclass(frozen=True)
class AcceptedInvitation:
    membership: PatientMembership
    already_member: bool = False


def _check_role(access, role):
    if role not in PatientMembership.Role.values:
        raise ValueError("请选择有效角色。")
    if role == "ADMIN" and not access.is_owner:
        raise PermissionDenied


@sensitive_variables()
def create_invitation(patient, actor, *, role, recipient_phone=None, recipient_account_id=None, label="", now=None):
    now = now or timezone.now()
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        _check_role(access, role)
        if bool(recipient_phone) == bool(recipient_account_id):
            raise ValueError("请指定一个接收手机号或账号。")
        phone_hash = hash_phone(normalize_mainland_phone(recipient_phone)) if recipient_phone else ""
        account_id = UUID(str(recipient_account_id)) if recipient_account_id else None
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise ValueError("成员称呼过长。")
        token, digest = create_token("invitation")
        invitation = PatientInvitation.objects.create(
            patient=access.patient, created_by=access.actor, creator_revision=access.membership.revision,
            role=role, label=label.strip(), recipient_phone_hash=phone_hash, recipient_account_id=account_id,
            token_digest=digest, created_at=now, expires_at=now + timedelta(days=7),
        )
        record_audit_event(access.actor.pk, "invitation_created", invitation.pk, "succeeded", patient_id=access.patient.pk)
        return CreatedInvitation(invitation, token)


def _validate(invitation, account, *, now):
    if (invitation.revoked_at is not None or invitation.accepted_at is not None
            or invitation.expires_at <= now or invitation.created_by_id is None or not account.is_active):
        raise InvitationUnavailable
    if invitation.recipient_account_id is not None:
        matches = invitation.recipient_account_id == account.pk
    else:
        matches = hmac.compare_digest(invitation.recipient_phone_hash, account.phone_hash)
    if not matches:
        raise InvitationUnavailable
    try:
        access = authorize_patient(invitation.patient_id, invitation.created_by_id, Capability.MANAGE, lock=True)
        _check_role(access, invitation.role)
    except PermissionDenied:
        raise InvitationUnavailable from None
    if access.membership.revision != invitation.creator_revision:
        raise InvitationUnavailable
    return access


@sensitive_variables()
def inspect_invitation(token, actor, *, now=None):
    with transaction.atomic():
        invitation, account, _ = _lock_invitation(token, actor)
        access = _validate(invitation, account, now=now or timezone.now())
        return invitation, access.patient


@sensitive_variables()
def _lock_invitation(token, actor):
    """Caller holds atomic; lock Account -> Patient -> invitation, then reread."""
    digest = token_digest(token, "invitation")
    candidate = PatientInvitation.objects.filter(token_digest=digest).values("pk", "patient_id").first()
    if candidate is None:
        raise InvitationUnavailable
    # A new relationship must not appear after account deletion enumerates
    # its patients. NO KEY UPDATE also permits existing actor FK writes.
    account = Account.objects.select_for_update(no_key=True).filter(pk=getattr(actor, "pk", actor), is_active=True).first()
    if account is None:
        raise InvitationUnavailable
    from .models import Patient
    patient = Patient.objects.select_for_update().filter(pk=candidate["patient_id"]).first()
    if patient is None:
        raise InvitationUnavailable
    invitation = PatientInvitation.objects.select_for_update().filter(pk=candidate["pk"], token_digest=digest).first()
    if invitation is None:
        raise InvitationUnavailable
    return invitation, account, patient


@sensitive_variables()
def accept_invitation(token, actor, *, now=None):
    with transaction.atomic():
        invitation, account, patient = _lock_invitation(token, actor)
        current_time = now or timezone.now()
        _validate(invitation, account, now=current_time)
        member = PatientMembership.objects.select_for_update().filter(patient=patient, account=account).first()
        already_member = member is not None and member.revoked_at is None
        if member is None:
            member = PatientMembership.objects.create(patient=patient, account=account, role=invitation.role, label=invitation.label)
        elif not already_member:
            member.revision += 1
            member.role, member.label, member.revoked_at = invitation.role, invitation.label, None
            member.save(update_fields=["revision", "role", "label", "revoked_at"])
        invitation.accepted_at, invitation.accepted_by = current_time, account
        invitation.save(update_fields=["accepted_at", "accepted_by"])
        record_audit_event(account.pk, "invitation_accepted", invitation.pk, "succeeded",
                           "existing_member" if already_member else "joined", patient_id=patient.pk)
    return AcceptedInvitation(member, already_member)


def revoke_invitation(patient, actor, invitation_id, *, now=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        invitation = PatientInvitation.objects.select_for_update().filter(pk=invitation_id, patient=access.patient).first()
        if invitation is None:
            raise PermissionDenied
        _check_role(access, invitation.role)
        if invitation.revoked_at is None:
            invitation.revoked_at = now or timezone.now()
            invitation.save(update_fields=["revoked_at"])
            record_audit_event(access.actor.pk, "invitation_revoked", invitation.pk, "succeeded", patient_id=access.patient.pk)
        return invitation
