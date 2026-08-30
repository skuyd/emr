import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.documents.errors import (
    ImmutableCollision,
    IntegrityMismatch,
    InvalidPresignExpiry,
    InvalidStorageReference,
    ObjectNotFound,
    StagingAccessDenied,
    StorageTransportError,
)
from apps.documents.storage import (
    ImmutableObject,
    LocalObjectStore,
    ObjectStore,
    S3ObjectStore,
    StagedObject,
)
from tests.documents.fakes import FakeS3Client


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def stage(store, payload):
    return store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=digest(payload))


def test_local_store_satisfies_the_private_object_store_protocol(tmp_path):
    assert isinstance(LocalObjectStore(tmp_path), ObjectStore)


def test_staging_is_opaque_and_cannot_be_read_or_presigned(tmp_path):
    store = LocalObjectStore(tmp_path)
    staged = stage(store, b"safe bytes")

    with pytest.raises(StagingAccessDenied):
        store.open_private(staged)
    with pytest.raises(StagingAccessDenied):
        store.presign_get(staged)


@pytest.mark.parametrize(
    ("expected_size", "expected_digest"),
    [(9, digest(b"safe bytes")), (10, "0" * 64)],
)
def test_local_staging_reverifies_exact_bytes_and_cleans_partial_file(tmp_path, expected_size, expected_digest):
    store = LocalObjectStore(tmp_path)
    with pytest.raises(IntegrityMismatch):
        store.put_staging(io.BytesIO(b"safe bytes"), expected_size=expected_size, expected_sha256=expected_digest)
    assert list((tmp_path / "staging").iterdir()) == []


def test_staging_reads_at_most_expected_size_plus_one(tmp_path):
    class Endless:
        total = 0

        def read(self, size):
            self.total += size
            return b"x" * size

    source = Endless()
    store = LocalObjectStore(tmp_path)
    with pytest.raises(IntegrityMismatch):
        store.put_staging(source, expected_size=10, expected_sha256=digest(b"x" * 10))
    assert source.total == 11
    assert list((tmp_path / "staging").iterdir()) == []


def test_local_promotion_is_create_only_and_private(tmp_path):
    store = LocalObjectStore(tmp_path)
    payload = b"immutable original"
    staged = stage(store, payload)

    promoted = store.promote_immutable(staged, "originals/aa/document.bin")

    assert promoted.created is True
    assert not (tmp_path / staged.key).exists()
    with store.open_private(promoted) as source:
        assert source.read() == payload


def test_same_digest_promotion_is_idempotent_without_overwrite(tmp_path):
    store = LocalObjectStore(tmp_path)
    first = store.promote_immutable(stage(store, b"same"), "originals/document.bin")
    second_staged = stage(store, b"same")

    second = store.promote_immutable(second_staged, first.key)

    assert first.created is True
    assert second.created is False
    assert not (tmp_path / second_staged.key).exists()
    with store.open_private(first) as source:
        assert source.read() == b"same"


def test_concurrent_local_loser_cannot_compensate_winners_original(tmp_path):
    store = LocalObjectStore(tmp_path)
    stages = [stage(store, b"same") for _ in range(2)]
    barrier = Barrier(2)

    def promote(staged):
        barrier.wait()
        return store.promote_immutable(staged, "originals/concurrent.bin")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(promote, stages))

    winner = next(item for item in results if item.created)
    loser = next(item for item in results if not item.created)
    assert store.compensate_promotion(loser) is False
    with pytest.raises(InvalidStorageReference):
        store.delete(loser)
    with store.open_private(winner) as source:
        assert source.read() == b"same"


def test_different_digest_collision_never_replaces_existing_original(tmp_path):
    store = LocalObjectStore(tmp_path)
    original = store.promote_immutable(stage(store, b"winner"), "originals/document.bin")
    losing_stage = stage(store, b"different")

    with pytest.raises(ImmutableCollision):
        store.promote_immutable(losing_stage, original.key)

    with store.open_private(original) as source:
        assert source.read() == b"winner"
    assert (tmp_path / losing_stage.key).exists()


def test_modified_staging_object_is_rejected_before_promotion(tmp_path):
    store = LocalObjectStore(tmp_path)
    staged = stage(store, b"verified")
    (tmp_path / staged.key).write_bytes(b"changed")
    with pytest.raises(IntegrityMismatch):
        store.promote_immutable(staged, "originals/document.bin")
    assert not (tmp_path / "originals" / "document.bin").exists()


def test_delete_is_idempotent_and_will_not_delete_changed_reference(tmp_path):
    store = LocalObjectStore(tmp_path)
    promoted = store.promote_immutable(stage(store, b"kept"), "originals/document.bin")
    wrong_reference = ImmutableObject(promoted.key, "0" * 64, promoted.byte_size, created=True)

    with pytest.raises(IntegrityMismatch):
        store.delete(wrong_reference)
    assert (tmp_path / promoted.key).exists()

    store.delete(promoted)
    store.delete(promoted)
    with pytest.raises(ObjectNotFound):
        store.open_private(promoted)


@pytest.mark.parametrize(
    "key",
    ["../outside", "originals/../outside", "originals\\outside", "/originals/outside", "public/outside"],
)
def test_invalid_or_nonprivate_keys_are_rejected(tmp_path, key):
    store = LocalObjectStore(tmp_path)
    with pytest.raises((InvalidStorageReference, StagingAccessDenied)):
        store.open_private(key)


def test_local_presign_is_opaque_short_lived_and_never_exposes_disk_path(tmp_path):
    store = LocalObjectStore(tmp_path, signing_key="test-only-secret", clock=lambda: 1_000)
    promoted = store.promote_immutable(stage(store, b"download"), "originals/document.bin")

    signed = store.presign_get(promoted, expires_in=300)

    assert signed.expires_at == 1_300
    assert signed.url.startswith("/private-objects/")
    assert str(tmp_path) not in signed.url
    assert "document.bin" not in signed.url
    parsed = urlsplit(signed.url)
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert store.resolve_presigned(parsed.path.rsplit("/", 1)[-1], **query) == promoted.key
    with pytest.raises(StagingAccessDenied):
        store.resolve_presigned(parsed.path.rsplit("/", 1)[-1], 999, "tampered")
    with pytest.raises(InvalidPresignExpiry):
        store.presign_get(promoted, expires_in=301)
    with pytest.raises(InvalidPresignExpiry):
        store.presign_get(promoted, expires_in=0)


def test_s3_staging_is_private_and_exact(tmp_path):
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket", prefix="tenant-app")

    staged = stage(store, b"s3 bytes")

    assert client.objects[f"tenant-app/{staged.key}"]["body"] == b"s3 bytes"
    operation, parameters = client.calls[0]
    assert operation == "put_object"
    assert "ACL" not in parameters
    assert parameters["IfNoneMatch"] == "*"
    assert parameters["Metadata"] == {"sha256": staged.sha256, "byte-size": str(staged.byte_size)}


def test_s3_promotion_uses_conditional_create_and_removes_staging():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket")
    staged = stage(store, b"original")

    promoted = store.promote_immutable(staged, "originals/document.bin")

    assert promoted.created is True
    assert staged.key not in client.objects
    assert client.objects[promoted.key]["body"] == b"original"
    final_put = [parameters for operation, parameters in client.calls if operation == "put_object"][-1]
    assert final_put["IfNoneMatch"] == "*"


def test_s3_same_digest_retry_is_idempotent_and_different_digest_collides():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket")
    first = store.promote_immutable(stage(store, b"same"), "originals/document.bin")

    repeated = store.promote_immutable(stage(store, b"same"), first.key)
    assert repeated.created is False
    assert store.compensate_promotion(repeated) is False
    with pytest.raises(InvalidStorageReference):
        store.delete(repeated)
    with store.open_private(first) as source:
        assert source.read() == b"same"

    losing_stage = stage(store, b"different")
    with pytest.raises(ImmutableCollision):
        store.promote_immutable(losing_stage, first.key)
    assert client.objects[first.key]["body"] == b"same"
    assert losing_stage.key in client.objects


def test_s3_promotion_reverifies_staged_bytes():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket")
    staged = stage(store, b"expected")
    client.objects[staged.key]["body"] = b"mutated"

    with pytest.raises(IntegrityMismatch):
        store.promote_immutable(staged, "originals/document.bin")
    assert "originals/document.bin" not in client.objects


def test_s3_open_delete_and_presign_remain_private_and_bounded():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket", clock=lambda: 2_000)
    promoted = store.promote_immutable(stage(store, b"content"), "originals/document.bin")

    with store.open_private(promoted) as source:
        assert source.read() == b"content"
    signed = store.presign_get(promoted, expires_in=120)
    assert signed.expires_at == 2_120
    assert "expires=120" in signed.url
    with pytest.raises(StagingAccessDenied):
        store.presign_get(StagedObject("staging/x", digest(b"x"), 1))

    store.delete(promoted)
    store.delete(promoted)
    with pytest.raises(ObjectNotFound):
        store.open_private(promoted)


def test_s3_transport_errors_are_typed_and_do_not_expose_backend_messages():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket")
    client.fail_once("put_object", code="InternalSecretFailure")

    with pytest.raises(StorageTransportError) as raised:
        stage(store, b"payload")

    assert str(raised.value) == "storage_transport_error"
    assert "InternalSecret" not in str(raised.value)


def test_ambiguous_s3_staging_put_never_deletes_a_server_accepted_object():
    client = FakeS3Client()
    store = S3ObjectStore(client, "private-bucket")
    client.write_then_lose_put_response_once()

    with pytest.raises(StorageTransportError):
        stage(store, b"accepted-before-timeout")

    assert len(client.objects) == 1
    key, stored = next(iter(client.objects.items()))
    assert key.startswith("staging/")
    assert stored["body"] == b"accepted-before-timeout"
