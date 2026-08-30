from django.utils import timezone
import pytest

from apps.accounts.deletion import purge_account_deletion, request_account_deletion
from apps.accounts.models import Account
from apps.documents.deletion import purge_document_deletion
from apps.documents.models import DocumentDeletionJob
from apps.operations.models import DeletionTombstone, TombstoneKind
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_account_purge_retains_only_irreversible_tombstones_and_audit(django_user_model):
    _client, patient = _patient(django_user_model, "7")
    account_id = patient.account_id
    document = _document(patient, content_type="image/png", page_count=1)[0]
    document_id = document.pk
    document_jobs = []
    account_jobs = []

    job = request_account_deletion(
        account_id,
        document_dispatch=document_jobs.append,
        account_dispatch=account_jobs.append,
        now=timezone.now(),
    )
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic-private-object"
    purge_document_deletion(DocumentDeletionJob.objects.get(document_id=document_id).pk, store)
    purge_account_deletion(job.pk)

    assert not Account.objects.filter(pk=account_id).exists()
    assert DeletionTombstone.objects.filter(kind=TombstoneKind.ACCOUNT).count() == 1
    assert DeletionTombstone.objects.filter(kind=TombstoneKind.DOCUMENT).count() == 1
    for tombstone in DeletionTombstone.objects.all():
        assert str(account_id) not in tombstone.target_hash
        assert str(document_id) not in tombstone.target_hash
