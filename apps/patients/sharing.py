"""Limited share capabilities, checked independently of family membership."""

from dataclasses import dataclass, field
from datetime import timedelta
import hmac

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import Account
from apps.documents.models import Document
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.sessions import account_session_is_active, session_digest
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from .access import Capability, authorize_patient
from .models import Patient, PatientShare, ShareSource, ShareViewerGrant
from .sharing_content import PARTIAL_KEYS, normalize_scope, project_snapshot
from .tokens import create_token, token_digest


class ShareUnavailable(PermissionDenied):
    def __init__(self):
        super().__init__("分享已到期、撤销或来源已变化，请联系分享人重新生成。")


@dataclass(frozen=True)
class CreatedShare:
    share: PatientShare
    token: str = field(repr=False)


@dataclass(frozen=True)
class ShareAccess:
    share: PatientShare
    actor: Account


def _hide(share, reason, *, now=None, actor="system"):
    newly_invalidated = share.invalidated_at is None
    if share.invalidated_at is None:
        share.invalidated_at = now or timezone.now()
        share.invalidation_reason = reason
    share.snapshot = {}
    share.snapshot_digest = ""
    share.save(update_fields=["invalidated_at", "invalidation_reason", "snapshot", "snapshot_digest"])
    if newly_invalidated:
        record_audit_event(actor, "share_expired" if reason == "expired" else "share_invalidated",
                           share.pk, "succeeded", reason, patient_id=share.patient_id)


def _lock_share(share_id):
    patient_id = PatientShare.objects.filter(pk=share_id).values_list("patient_id", flat=True).first()
    # Source author anonymization may need a deferred Patient KEY SHARE check.
    patient = Patient.objects.select_for_update(no_key=True).filter(pk=patient_id).first()
    if patient is None:
        raise PermissionDenied
    # Every mutation of the share also takes this Patient guard. Source locks
    # are acquired by assert_snapshot_current before any later child update.
    share = PatientShare.objects.filter(pk=share_id, patient=patient).first()
    if share is None:
        raise PermissionDenied
    return share, patient


def _validate_locked(share, patient, *, now=None):
    now = now or timezone.now()
    reason = ""
    if share.revoked_at is not None or share.invalidated_at is not None:
        reason = share.invalidation_reason or "revoked"
    elif share.expires_at <= now:
        reason = "expired"
    else:
        try:
            access = authorize_patient(patient, share.created_by_id, Capability.MANAGE)
            if access.membership.revision != share.creator_revision:
                reason = "creator_changed"
        except PermissionDenied:
            reason = "creator_unavailable"
    if not reason:
        if not share.snapshot or not hmac.compare_digest(digest(share.snapshot), share.snapshot_digest):
            reason = "snapshot_changed"
        else:
            try:
                selected = set(share.scope["document_ids"])
                revisions = {str(identity): revision for identity, revision in share.source_bindings.values_list(
                    "document_id", "document__material_revision")}
                record_ids = set(share.scope.get('self_record_ids', []))
                record_bindings = {str(identity) for identity in share.self_record_sources.values_list('record_id', flat=True)}
                from apps.exports.treatment import bindings_current
                from apps.glucose.output import bindings_current as glucose_bindings_current
                from apps.lesions.portable import bindings_current as lesion_bindings_current
                if (selected != set(revisions) or {row["id"] for row in share.snapshot["documents"]} != selected
                        or share.snapshot.get("source_material_revisions") != revisions
                        or record_ids != record_bindings or {row['id'] for row in share.snapshot.get('self_records', [])} != record_ids
                        or not bindings_current(share, share.snapshot)
                        or not glucose_bindings_current(share, share.snapshot)
                        or not lesion_bindings_current(share, share.snapshot)):
                    reason = "source_changed"
                else:
                    assert_snapshot_current(patient, share.snapshot)
            except (PermissionDenied, SnapshotChanged, ExportInputError, KeyError, TypeError):
                reason = "source_changed"
    if reason:
        if share.snapshot or share.invalidated_at is None:
            _hide(share, reason, now=now)
        return False
    return True


@sensitive_variables()
def create_share(patient, actor, selection, *, allow_original_download=False, expires_in_hours=24, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        if type(expires_in_hours) is not int or not 1 <= expires_in_hours <= 168:
            raise ExportInputError("分享有效期应为 1 至 168 小时。")
        if type(allow_original_download) is not bool:
            raise ExportInputError("原件下载许可无效。")
        scope = normalize_scope(selection)
        if allow_original_download and ("sources" not in scope["sections"] or any(key in scope for key in PARTIAL_KEYS)):
            raise ExportInputError("只有开放完整原件来源时才能允许原件下载。")
        frozen = build_snapshot(access.patient, scope, now=now)
        projection = project_snapshot(frozen, scope)
        projection["source_material_revisions"] = {
            str(identity): revision for identity, revision in Document.objects.filter(
                patient=access.patient, pk__in=scope["document_ids"],
            ).values_list("pk", "material_revision")
        }
        token, secret_digest = create_token("share")
        share = PatientShare.objects.create(
            patient=access.patient, created_by=access.actor, creator_revision=access.membership.revision,
            token_digest=secret_digest, scope=scope, snapshot=projection, snapshot_digest=digest(projection),
            allow_original_download=allow_original_download, created_at=now, expires_at=now + timedelta(hours=expires_in_hours),
        )
        ShareSource.objects.bulk_create([ShareSource(share=share, document_id=identity) for identity in scope["document_ids"]])
        from apps.self_records.models import DailyRecordShareSource
        DailyRecordShareSource.objects.bulk_create([
            DailyRecordShareSource(share=share, record_id=identity) for identity in scope.get('self_record_ids', [])
        ])
        from apps.exports.treatment import bind_output
        bind_output(share, projection, sharing=True)
        from apps.glucose.output import bind_output as bind_glucose
        bind_glucose(share, projection, sharing=True)
        from apps.lesions.portable import bind_output as bind_lesions
        bind_lesions(share, projection, sharing=True)
        record_audit_event(access.actor.pk, "share_created", share.pk, "succeeded", patient_id=access.patient.pk)
    return CreatedShare(share, token)


@sensitive_variables()
def exchange_share_token(token, actor, key, *, now=None):
    identity = PatientShare.objects.filter(token_digest=token_digest(token, "share")).values_list("pk", flat=True).first()
    if identity is None:
        raise ShareUnavailable
    valid = False
    with transaction.atomic():
        account = Account.objects.select_for_update(no_key=True).filter(pk=getattr(actor, "pk", actor), is_active=True).first()
        if account is None or not key or not account_session_is_active(account, session_digest(key), now=now):
            raise PermissionDenied
        share, patient = _lock_share(identity)
        valid = _validate_locked(share, patient, now=now)
        if valid:
            grant, _ = ShareViewerGrant.objects.get_or_create(
                share=share, account=account, session_digest=session_digest(key),
            )
            record_audit_event(account.pk, "share_access_granted", share.pk, "succeeded", patient_id=patient.pk)
    # Scrub any invalidated snapshot before raising, rather than rolling that
    # cleanup back with the denied access.
    if not valid:
        raise ShareUnavailable
    return grant


def authorize_share(share_id, actor, key, *, document_id=None, sources=False, download=False, now=None):
    account_id = getattr(actor, "pk", actor)
    if not key or not ShareViewerGrant.objects.filter(share_id=share_id, account_id=account_id, session_digest=session_digest(key)).exists():
        raise PermissionDenied
    valid = False
    with transaction.atomic():
        share, patient = _lock_share(share_id)
        account = Account.objects.filter(pk=account_id, is_active=True).first()
        if account is None or not account_session_is_active(account, session_digest(key), now=now):
            raise PermissionDenied
        # Reread the grant after obtaining the lifecycle guard too.
        if not ShareViewerGrant.objects.filter(share=share, account=account, session_digest=session_digest(key)).exists():
            raise PermissionDenied
        valid = _validate_locked(share, patient, now=now)
        if valid:
            if document_id is not None and str(document_id) not in share.scope["document_ids"]:
                raise PermissionDenied
            if (sources or download) and "sources" not in share.scope["sections"]:
                raise PermissionDenied
            if download and not share.allow_original_download:
                raise PermissionDenied
    if not valid:
        raise ShareUnavailable
    return ShareAccess(share, account)


def revoke_share(patient, actor, share_id, *, now=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        share = PatientShare.objects.select_for_update().filter(pk=share_id, patient=access.patient).first()
        if share is None:
            raise PermissionDenied
        if share.revoked_at is None:
            share.revoked_at = now or timezone.now()
            share.save(update_fields=["revoked_at"])
            record_audit_event(access.actor.pk, "share_revoked", share.pk, "succeeded", patient_id=access.patient.pk)
        _hide(share, "revoked", now=now, actor=access.actor.pk)
        return share


def validate_managed_share(patient, actor, share_id, *, now=None):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.MANAGE, lock=True)
        share, current = _lock_share(share_id)
        if current.pk != access.patient.pk:
            raise PermissionDenied
        _validate_locked(share, current, now=now)
        return share


def invalidate_document_shares(document):
    from django.db.models import Q
    affected = PatientShare.objects.filter(Q(source_bindings__document=document) | Q(treatment_sources__document=document)
                                          | Q(glucose_sources__record__source_document=document)
                                          | Q(lesion_sources__observation__document=document)).values("pk")
    for share in PatientShare.objects.filter(pk__in=affected, invalidated_at__isnull=True).order_by("pk"):
        _hide(share, "source_unavailable")


def invalidate_member_shares(patient, account_id):
    for share in PatientShare.objects.filter(patient=patient, created_by_id=account_id, invalidated_at__isnull=True).order_by("pk"):
        _hide(share, "creator_changed")


def invalidate_patient_shares(patient):
    for share in PatientShare.objects.filter(patient=patient, invalidated_at__isnull=True).order_by("pk"):
        _hide(share, "patient_unavailable")


def expire_shares(*, now=None, limit=100):
    now = now or timezone.now()
    identities = list(PatientShare.objects.filter(expires_at__lte=now, invalidated_at__isnull=True).order_by("expires_at", "pk").values_list("pk", flat=True)[:limit])
    for identity in identities:
        with transaction.atomic():
            try:
                share, _ = _lock_share(identity)
            except PermissionDenied:
                continue
            _hide(share, "expired", now=now)
    return len(identities)
