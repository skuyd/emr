"""Live patient authorization shared by HTTP, domain services and workers.

Mutation callers acquire the Patient guard before child rows. Revocation uses
the same guard, so an operation either commits before revocation or is denied.
"""

from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import Patient, PatientMembership


class Capability(StrEnum):
    READ = "read"
    WRITE = "write"
    EXPORT = "export"
    MANAGE = "manage"
    OWNER = "owner"


@dataclass(frozen=True)
class PatientAccess:
    patient: Patient
    actor: object
    membership: PatientMembership

    @property
    def is_owner(self):
        return self.patient.account_id == self.actor.pk

    def permits(self, capability):
        capability = Capability(capability)
        if self.is_owner:
            return True
        if capability == Capability.OWNER:
            return False
        if capability == Capability.MANAGE:
            return self.membership.role == "ADMIN"
        if capability in {Capability.WRITE, Capability.EXPORT}:
            return self.membership.role in {"ADMIN", "EDITOR"}
        return True


def accessible_patients(actor):
    return Patient.objects.filter(
        deleted_at__isnull=True, account__is_active=True,
        memberships__account_id=actor.pk, memberships__account__is_active=True,
        memberships__revoked_at__isnull=True,
    ).order_by("created_at", "pk")


def authorize_patient(patient, actor, capability=Capability.READ, *, lock=False):
    from apps.accounts.models import Account

    identity = getattr(patient, "pk", patient)
    # Serialize all patient mutations and access revocation, while allowing
    # deferred child FK checks to finish (e.g. author SET_NULL during purge).
    # FOR UPDATE here can deadlock while we wait for that same child row.
    query = Patient.objects.select_for_update(of=("self",), no_key=True) if lock else Patient.objects
    current = query.filter(pk=identity, deleted_at__isnull=True, account__is_active=True).first()
    account = Account.objects.filter(pk=getattr(actor, "pk", actor), is_active=True).first()
    if current is None or account is None:
        raise PermissionDenied("患者资料不可用或访问资格已撤销。")
    membership = PatientMembership.objects.filter(
        patient=current, account=account, revoked_at__isnull=True,
    ).first()
    if membership is None or membership.role not in PatientMembership.Role.values:
        raise PermissionDenied("患者资料不可用或访问资格已撤销。")
    access = PatientAccess(current, account, membership)
    if not access.permits(capability):
        raise PermissionDenied("当前成员角色不允许此操作。")
    return access


def owner_actor(patient, actor):
    """Compatibility for trusted owner-only domain calls; HTTP always supplies actor."""
    return actor if actor is not None else patient.account_id


def change_membership(patient, actor, membership_id, *, role=None, revoke=False, expected_revision):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        member = PatientMembership.objects.select_for_update().filter(pk=membership_id, patient=access.patient).first()
        if member is None or member.account_id == access.patient.account_id:
            raise PermissionDenied
        if member.role == "ADMIN" or role == "ADMIN":
            if not access.is_owner:
                raise PermissionDenied
        if member.revision != expected_revision or member.revoked_at is not None:
            raise ValueError("成员已更新，请刷新后重试。")
        if not revoke and role not in PatientMembership.Role.values:
            raise ValueError("请选择有效角色。")
        member.revision += 1
        if revoke:
            member.revoked_at = timezone.now()
        else:
            member.role = role
        member.save(update_fields=["role", "revision", "revoked_at"])
        invalidate_member_access(access.patient, member.account_id, actor=access.actor)
        from apps.operations.audit import record_audit_event
        record_audit_event(access.actor.pk, "member_access_revoked" if revoke else "member_role_changed",
                           member.pk, "succeeded")
        return member


def invalidate_member_access(patient, account_id, *, actor=None):
    from .sharing import invalidate_member_shares
    from apps.exports.services import invalidate_member_exports
    from apps.notifications.services import revoke_push_subscriptions
    from apps.labs.models import ReviewTask, ReviewTaskEvent, ReviewTaskStatus

    invalidate_member_exports(patient, account_id)
    invalidate_member_shares(patient, account_id)
    revoke_push_subscriptions(patient, account_id=account_id)
    from apps.processing.reprocessing import invalidate_member_reprocessing
    invalidate_member_reprocessing(patient, account_id)
    for task in ReviewTask.objects.select_for_update().filter(
        observation__parsing_version__document__patient=patient,
        granted_by_id=account_id, revoked_at__isnull=True,
    ).order_by("pk"):
        before = task.status
        task.status = ReviewTaskStatus.REVOKED
        task.revoked_at = timezone.now()
        task.revision_number += 1
        task.save(update_fields=["status", "revoked_at", "revision_number", "updated_at"])
        ReviewTaskEvent.objects.create(task=task, author_id=getattr(actor, "pk", actor), sequence=task.revision_number,
                                      action="MEMBER_REVOKED", before_status=before, after_status=task.status)
