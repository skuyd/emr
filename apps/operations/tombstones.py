from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
import hashlib
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import DeletionTombstone, TombstoneKind


_DIGEST = re.compile(r"[0-9a-f]{64}")
_ENTRY_KEYS = {"version", "kind", "target_hash", "requested_at", "signature"}
MAX_LOG_ENTRIES = 1_000_000
MAX_LOG_LINE_BYTES = 4096


class InvalidTombstoneLog(ValueError):
    pass


@dataclass(frozen=True)
class RestoreReplayResult:
    imported: int
    accounts_hidden: int
    documents_hidden: int
    patients_hidden: int = 0


def _key(setting_name):
    value = getattr(settings, setting_name, "")
    if not isinstance(value, str) or not value:
        raise InvalidTombstoneLog("Tombstone key is unavailable")
    return value.encode("utf-8")


def tombstone_target_hash(kind, target_id):
    if kind not in TombstoneKind.values:
        raise InvalidTombstoneLog("Unknown tombstone kind")
    try:
        canonical = str(uuid.UUID(str(target_id)))
    except (TypeError, ValueError, AttributeError):
        raise InvalidTombstoneLog("Tombstone targets must be opaque UUIDs") from None
    return hmac.new(
        _key("TOMBSTONE_HASH_KEY"),
        f"family-phr/tombstone/{kind}/v1:{canonical}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_time(value):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise InvalidTombstoneLog("Tombstone time must be timezone-aware")
    return value.astimezone(datetime_timezone.utc).isoformat(timespec="microseconds")


def _signature(kind, target_hash, requested_at):
    message = f"v1|{kind}|{target_hash}|{_canonical_time(requested_at)}".encode("ascii")
    return hmac.new(_key("TOMBSTONE_SIGNING_KEY"), message, hashlib.sha256).hexdigest()


def record_deletion_tombstone(kind, target_id, *, now=None):
    now = now or timezone.now()
    digest = tombstone_target_hash(kind, target_id)
    signature = _signature(kind, digest, now)
    tombstone, _created = DeletionTombstone.objects.get_or_create(
        kind=kind,
        target_hash=digest,
        defaults={"requested_at": now, "signature": signature},
    )
    return tombstone


def _serialize(tombstone):
    expected = _signature(tombstone.kind, tombstone.target_hash, tombstone.requested_at)
    if not hmac.compare_digest(expected, tombstone.signature):
        raise InvalidTombstoneLog("Stored tombstone signature is invalid")
    return {
        "version": 1,
        "kind": tombstone.kind,
        "target_hash": tombstone.target_hash,
        "requested_at": _canonical_time(tombstone.requested_at),
        "signature": tombstone.signature,
    }


def export_tombstone_entries():
    return [
        _serialize(tombstone)
        for tombstone in DeletionTombstone.objects.order_by("created_at", "pk").iterator()
    ]


def encode_tombstone_log(entries=None):
    entries = export_tombstone_entries() if entries is None else entries
    validated = []
    for entry in entries:
        _validate_entry(entry)
        validated.append(entry)
    return "".join(
        json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        for entry in validated
    )


def decode_tombstone_log(content):
    if not isinstance(content, str):
        raise InvalidTombstoneLog("Tombstone log must be text")
    entries = []
    for line in content.splitlines():
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LOG_LINE_BYTES or len(entries) >= MAX_LOG_ENTRIES:
            raise InvalidTombstoneLog("Tombstone log exceeds its bounded format")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise InvalidTombstoneLog("Tombstone log contains invalid JSON") from None
        _validate_entry(entry)
        entries.append(entry)
    return entries


def merge_tombstone_entries(*entry_groups):
    merged = {}
    for entries in entry_groups:
        for entry in entries:
            kind, target_hash, requested_at, _signature_value = _validate_entry(entry)
            key = (kind, target_hash)
            previous = merged.get(key)
            if previous is None:
                merged[key] = entry
                continue
            previous_time = datetime.fromisoformat(previous["requested_at"])
            if requested_at < previous_time:
                merged[key] = entry
    return sorted(merged.values(), key=lambda item: (item["requested_at"], item["kind"], item["target_hash"]))


def _validate_entry(entry):
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS or entry.get("version") != 1:
        raise InvalidTombstoneLog("Invalid tombstone entry shape")
    kind = entry.get("kind")
    target_hash = entry.get("target_hash")
    signature = entry.get("signature")
    if kind not in TombstoneKind.values or _DIGEST.fullmatch(target_hash or "") is None:
        raise InvalidTombstoneLog("Invalid tombstone identifier")
    if _DIGEST.fullmatch(signature or "") is None:
        raise InvalidTombstoneLog("Invalid tombstone signature")
    try:
        requested_at = datetime.fromisoformat(entry["requested_at"])
    except (TypeError, ValueError):
        raise InvalidTombstoneLog("Invalid tombstone time") from None
    expected = _signature(kind, target_hash, requested_at)
    if not hmac.compare_digest(expected, signature):
        raise InvalidTombstoneLog("Tombstone signature verification failed")
    return kind, target_hash, requested_at, signature


def import_tombstone_entries(entries):
    if not isinstance(entries, (list, tuple)):
        raise InvalidTombstoneLog("Tombstone log must be a list")
    validated = [_validate_entry(entry) for entry in entries]
    imported = 0
    with transaction.atomic():
        for kind, target_hash, requested_at, signature in validated:
            _tombstone, created = DeletionTombstone.objects.get_or_create(
                kind=kind,
                target_hash=target_hash,
                defaults={"requested_at": requested_at, "signature": signature},
            )
            imported += int(created)
    return imported


def replay_restore_tombstones(entries, *, document_dispatch, account_dispatch, now=None):
    from apps.accounts.deletion import AccountDeletionUnavailable, request_account_deletion
    from apps.accounts.models import Account
    from apps.documents.deletion import DeletionRequestUnavailable, request_document_deletion
    from apps.documents.models import Document

    now = now or timezone.now()
    imported = import_tombstone_entries(entries)
    account_hashes = set(
        DeletionTombstone.objects.filter(kind=TombstoneKind.ACCOUNT).values_list("target_hash", flat=True)
    )
    document_hashes = set(
        DeletionTombstone.objects.filter(kind=TombstoneKind.DOCUMENT).values_list("target_hash", flat=True)
    )
    accounts_hidden = 0
    documents_hidden = 0
    account_ids = tuple(Account.objects.filter(is_active=True).values_list("pk", flat=True))
    for account_id in account_ids:
        if tombstone_target_hash(TombstoneKind.ACCOUNT, account_id) not in account_hashes:
            continue
        try:
            request_account_deletion(
                account_id,
                document_dispatch=document_dispatch,
                account_dispatch=account_dispatch,
                now=now,
            )
        except AccountDeletionUnavailable:
            continue
        accounts_hidden += 1

    from apps.patients.models import Patient
    from apps.patients.deletion import request_patient_deletion
    patient_hashes = set(DeletionTombstone.objects.filter(kind=TombstoneKind.PATIENT).values_list("target_hash", flat=True))
    patients_hidden = 0
    for patient in Patient.objects.filter(deleted_at__isnull=True, account__is_active=True).iterator():
        if tombstone_target_hash(TombstoneKind.PATIENT, patient.pk) in patient_hashes:
            request_patient_deletion(patient.pk, patient.account, document_dispatch=document_dispatch, now=now)
            patients_hidden += 1
    documents = Document.objects.filter(
        models.Q(deleted_at__isnull=True) | models.Q(trashed_at__isnull=False)
    ).select_related("patient")
    for document in documents.iterator():
        if tombstone_target_hash(TombstoneKind.DOCUMENT, document.pk) not in document_hashes:
            continue
        try:
            request_document_deletion(
                document.patient,
                document.pk,
                dispatch=document_dispatch,
                now=now,
            )
        except DeletionRequestUnavailable:
            continue
        documents_hidden += 1
    return RestoreReplayResult(imported, accounts_hidden, documents_hidden, patients_hidden)
