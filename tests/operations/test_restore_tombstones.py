import json
import uuid

from django.conf import settings
from django.core.management import call_command, CommandError
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.accounts.models import Account
from apps.documents.models import Document
from apps.operations.models import DeletionTombstone, TombstoneKind
from apps.operations.tombstones import (
    decode_tombstone_log,
    encode_tombstone_log,
    InvalidTombstoneLog,
    export_tombstone_entries,
    record_deletion_tombstone,
    replay_restore_tombstones,
)
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_restore_replay_disables_restored_account_and_is_idempotent(
    django_user_model, django_capture_on_commit_callbacks
):
    _client, patient = _patient(django_user_model, "8")
    account = patient.account
    document = _document(patient, content_type="image/png", page_count=1)[0]
    record_deletion_tombstone(TombstoneKind.ACCOUNT, account.pk)
    entries = export_tombstone_entries()
    serialized = json.dumps(entries, ensure_ascii=False)

    assert str(account.pk) not in serialized
    assert str(document.pk) not in serialized
    assert patient.display_name not in serialized

    DeletionTombstone.objects.all().delete()
    document_dispatches = []
    account_dispatches = []
    with django_capture_on_commit_callbacks(execute=True):
        first = replay_restore_tombstones(
            entries,
            document_dispatch=document_dispatches.append,
            account_dispatch=account_dispatches.append,
        )
        second = replay_restore_tombstones(
            entries,
            document_dispatch=document_dispatches.append,
            account_dispatch=account_dispatches.append,
        )

    account.refresh_from_db()
    document.refresh_from_db()
    assert account.is_active is False
    assert document.deleted_at is not None
    assert first.accounts_hidden == 1
    assert second.accounts_hidden == 0
    assert len(account_dispatches) == 1
    assert len(document_dispatches) == 1
    assert DeletionTombstone.objects.filter(kind=TombstoneKind.ACCOUNT).count() == 1


def test_restore_replay_hides_standalone_document_tombstone(
    django_user_model, django_capture_on_commit_callbacks
):
    _client, patient = _patient(django_user_model, "9")
    document = _document(patient, content_type="image/png", page_count=1)[0]
    record_deletion_tombstone(TombstoneKind.DOCUMENT, document.pk)
    entries = export_tombstone_entries()
    DeletionTombstone.objects.all().delete()
    dispatched = []

    with django_capture_on_commit_callbacks(execute=True):
        result = replay_restore_tombstones(
            entries,
            document_dispatch=dispatched.append,
            account_dispatch=lambda _job_id: None,
        )

    document.refresh_from_db()
    assert document.deleted_at is not None
    assert result.documents_hidden == 1
    assert len(dispatched) == 1


def test_restore_replay_rejects_tampered_or_unknown_entries(django_user_model):
    _client, patient = _patient(django_user_model, "0")
    record_deletion_tombstone(TombstoneKind.ACCOUNT, patient.account_id)
    entry = export_tombstone_entries()[0]

    for mutation in (
        {**entry, "signature": "0" * 64},
        {**entry, "kind": "UNKNOWN"},
        {**entry, "extra": str(uuid.uuid4())},
    ):
        with pytest.raises(InvalidTombstoneLog):
            replay_restore_tombstones(
                [mutation],
                document_dispatch=lambda _job_id: None,
                account_dispatch=lambda _job_id: None,
                now=timezone.now(),
            )

    patient.account.refresh_from_db()
    assert patient.account.is_active is True


def test_external_tombstone_jsonl_round_trip_is_bounded_and_signed(django_user_model):
    _client, patient = _patient(django_user_model, "1")
    record_deletion_tombstone(TombstoneKind.ACCOUNT, patient.account_id)

    encoded = encode_tombstone_log()
    decoded = decode_tombstone_log(encoded)

    assert decoded == export_tombstone_entries()
    with pytest.raises(InvalidTombstoneLog):
        decode_tombstone_log("{" + "x" * 5000)


def test_synchronous_restore_replay_requires_isolated_targets(tmp_path, django_user_model):
    _client, patient = _patient(django_user_model, "2")
    record_deletion_tombstone(TombstoneKind.ACCOUNT, patient.account_id)
    source = tmp_path / "tombstones.jsonl"
    source.write_text(encode_tombstone_log(), encoding="utf-8")

    with pytest.raises(CommandError, match="isolated"):
        call_command(
            "replay_deletion_tombstones",
            str(source),
            synchronous=True,
            confirm="ISOLATED-RESTORE-DRILL",
        )


def test_synchronous_restore_replay_purges_only_isolated_database_and_storage(
    tmp_path, django_user_model, monkeypatch
):
    _client, patient = _patient(django_user_model, "3")
    account_id = patient.account_id
    document = _document(patient, content_type="image/png", page_count=1)[0]
    document_id = document.pk
    record_deletion_tombstone(TombstoneKind.ACCOUNT, account_id)
    source = tmp_path / "tombstones.jsonl"
    source.write_text(encode_tombstone_log(), encoding="utf-8")
    DeletionTombstone.objects.all().delete()

    monkeypatch.setitem(settings.DATABASES["default"], "NAME", "phr_restore_drill")
    with override_settings(
        RESTORE_DRILL_MODE=True,
        DOCUMENT_STORAGE_BACKEND="local",
        DOCUMENT_STORAGE_ROOT=tmp_path / "objects",
        DOCUMENT_S3_PREFIX="restore-drill/pytest",
    ):
        call_command(
            "replay_deletion_tombstones",
            str(source),
            synchronous=True,
            confirm="ISOLATED-RESTORE-DRILL",
        )

    assert not django_user_model.objects.filter(pk=account_id).exists()
    assert not Document.objects.filter(pk=document_id).exists()
