import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import io
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tempfile
import time
from typing import BinaryIO, Protocol, runtime_checkable
import uuid

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from .errors import (
    ImmutableCollision,
    IntegrityMismatch,
    InvalidPresignExpiry,
    InvalidStorageReference,
    ObjectNotFound,
    StagingAccessDenied,
    StorageTransportError,
)


_CHUNK_BYTES = 64 * 1024
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_KEY_PATTERN = re.compile(r"(?:staging|originals)/[A-Za-z0-9][A-Za-z0-9._/-]{0,500}")


@dataclass(frozen=True)
class StagedObject:
    key: str
    sha256: str
    byte_size: int
    etag: str = ""
    version_id: str = ""


@dataclass(frozen=True)
class ImmutableObject:
    key: str
    sha256: str
    byte_size: int
    created: bool
    etag: str = ""
    version_id: str = ""


@dataclass(frozen=True)
class PresignedDownload:
    url: str = field(repr=False)
    expires_at: int


@runtime_checkable
class ObjectStore(Protocol):
    def put_staging(self, source: BinaryIO, *, expected_size: int, expected_sha256: str, staging_key: str | None = None) -> StagedObject: ...

    def promote_immutable(self, staged: StagedObject, final_key: str) -> ImmutableObject: ...

    def compensate_promotion(self, item: ImmutableObject) -> bool: ...

    def open_private(self, item: ImmutableObject | str) -> BinaryIO: ...

    def delete(self, item: StagedObject | ImmutableObject | str) -> None: ...

    def presign_get(self, item: ImmutableObject | str, expires_in: int = 300) -> PresignedDownload: ...


def _validate_digest_size(expected_size, expected_sha256):
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise IntegrityMismatch()
    if not isinstance(expected_sha256, str) or _DIGEST_PATTERN.fullmatch(expected_sha256) is None:
        raise IntegrityMismatch()


def _validate_key(key, required_prefix=None):
    if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None or "\\" in key:
        raise InvalidStorageReference()
    path = PurePosixPath(key)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidStorageReference()
    prefix = path.parts[0]
    if required_prefix is not None and prefix != required_prefix:
        if prefix == "staging" and required_prefix == "originals":
            raise StagingAccessDenied()
        raise InvalidStorageReference()
    return key


def _reference_key(item):
    if isinstance(item, (StagedObject, ImmutableObject)):
        return item.key
    return item


def _validate_reference_metadata(item):
    if isinstance(item, (StagedObject, ImmutableObject)):
        _validate_digest_size(item.byte_size, item.sha256)


def _validate_expiry(expires_in):
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or not 1 <= expires_in <= 300:
        raise InvalidPresignExpiry()
    return expires_in


def _copy_verified(source, target, expected_size, expected_sha256):
    _validate_digest_size(expected_size, expected_sha256)
    digest = hashlib.sha256()
    size = 0
    while True:
        remaining = expected_size + 1 - size
        if remaining <= 0:
            raise IntegrityMismatch()
        request_size = min(_CHUNK_BYTES, remaining)
        try:
            chunk = source.read(request_size)
        except Exception:
            raise StorageTransportError() from None
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise IntegrityMismatch()
        chunk = bytes(chunk)
        if len(chunk) > request_size:
            raise IntegrityMismatch()
        if not chunk:
            break
        size += len(chunk)
        if size > expected_size:
            raise IntegrityMismatch()
        digest.update(chunk)
        try:
            target.write(chunk)
        except Exception:
            raise StorageTransportError() from None
    if size != expected_size or not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise IntegrityMismatch()
    return size, digest.hexdigest()


def _stream_digest(source):
    digest = hashlib.sha256()
    size = 0
    while True:
        try:
            chunk = source.read(_CHUNK_BYTES)
        except Exception:
            raise StorageTransportError() from None
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise IntegrityMismatch()
        chunk = bytes(chunk)
        if not chunk:
            return size, digest.hexdigest()
        size += len(chunk)
        digest.update(chunk)


class LocalObjectStore:
    """Private filesystem adapter for development and single-node deployments."""

    def __init__(self, root, *, signing_key=None, base_url="/private-objects", clock=time.time):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key or secrets.token_bytes(32)
        self._base_url = base_url.rstrip("/")
        for directory in (self.root / "staging", self.root / "originals"):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    def _path(self, key, required_prefix=None):
        key = _validate_key(key, required_prefix)
        candidate = (self.root / Path(*PurePosixPath(key).parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise InvalidStorageReference() from None
        return candidate

    def put_staging(self, source, *, expected_size, expected_sha256, staging_key=None):
        _validate_digest_size(expected_size, expected_sha256)
        key = _validate_key(staging_key, "staging") if staging_key is not None else f"staging/{uuid.uuid4().hex}"
        path = self._path(key, "staging")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    size, digest = _copy_verified(source, target, expected_size, expected_sha256)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            return StagedObject(key=key, sha256=digest, byte_size=size)
        except FileExistsError:
            raise ImmutableCollision() from None
        except (IntegrityMismatch, StorageTransportError):
            path.unlink(missing_ok=True)
            raise
        except OSError:
            path.unlink(missing_ok=True)
            raise StorageTransportError() from None

    def _matches(self, path, item):
        try:
            with path.open("rb") as source:
                size, digest = _stream_digest(source)
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageTransportError() from None
        return size == item.byte_size and hmac.compare_digest(digest, item.sha256)

    def promote_immutable(self, staged, final_key):
        if not isinstance(staged, StagedObject):
            raise InvalidStorageReference()
        _validate_reference_metadata(staged)
        source = self._path(staged.key, "staging")
        target = self._path(final_key, "originals")
        if not self._matches(source, staged):
            if not source.exists():
                raise ObjectNotFound()
            raise IntegrityMismatch()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
            created = True
        except FileExistsError:
            if not self._matches(target, staged):
                raise ImmutableCollision() from None
            created = False
        except OSError:
            raise StorageTransportError() from None
        try:
            source.unlink(missing_ok=True)
        except OSError:
            # The immutable object is already durable. A later staging sweeper can
            # remove this private orphan without invalidating the success result.
            pass
        return ImmutableObject(
            key=final_key,
            sha256=staged.sha256,
            byte_size=staged.byte_size,
            created=created,
        )

    def open_private(self, item):
        key = _reference_key(item)
        path = self._path(key, "originals")
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise ObjectNotFound() from None
        except OSError:
            raise StorageTransportError() from None

    def delete(self, item):
        if isinstance(item, ImmutableObject) and not item.created:
            raise InvalidStorageReference()
        key = _reference_key(item)
        path = self._path(key)
        _validate_reference_metadata(item)
        if isinstance(item, (StagedObject, ImmutableObject)) and path.exists() and not self._matches(path, item):
            raise IntegrityMismatch()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise StorageTransportError() from None

    def compensate_promotion(self, item):
        if not isinstance(item, ImmutableObject):
            raise InvalidStorageReference()
        if not item.created:
            return False
        self.delete(item)
        return True

    def presign_get(self, item, expires_in=300):
        expires_in = _validate_expiry(expires_in)
        key = _validate_key(_reference_key(item), "originals")
        expires_at = int(self._clock()) + expires_in
        token = base64.urlsafe_b64encode(key.encode("ascii")).decode("ascii").rstrip("=")
        message = f"{token}.{expires_at}".encode("ascii")
        signature = hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
        return PresignedDownload(
            url=f"{self._base_url}/{token}?expires={expires_at}&signature={signature}",
            expires_at=expires_at,
        )

    def resolve_presigned(self, token, expires, signature):
        """Validate a local download token and return its private object key."""
        try:
            expires = int(expires)
            if expires < int(self._clock()):
                raise ValueError
            message = f"{token}.{expires}".encode("ascii")
            expected = hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(signature)):
                raise ValueError
            padding = "=" * (-len(token) % 4)
            key = base64.b64decode(token + padding, altchars=b"-_", validate=True).decode("ascii")
            return _validate_key(key, "originals")
        except (UnicodeError, ValueError, TypeError, InvalidStorageReference, StagingAccessDenied):
            raise StagingAccessDenied() from None


def _client_error_code(error):
    if not isinstance(error, ClientError):
        return ""
    return str(error.response.get("Error", {}).get("Code", ""))


def _is_missing(error):
    return _client_error_code(error) in {"NoSuchKey", "NotFound", "404"}


def _is_precondition(error):
    return _client_error_code(error) in {"PreconditionFailed", "ConditionalRequestConflict", "409", "412"}


class S3ObjectStore:
    """S3-compatible private adapter using conditional create-only writes."""

    def __init__(self, client, bucket, *, prefix="", clock=time.time, put_options=None):
        if not bucket:
            raise InvalidStorageReference()
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._clock = clock
        self._put_options = dict(put_options or {})
        if "ACL" in self._put_options:
            raise InvalidStorageReference()

    def _full_key(self, key, required_prefix=None):
        key = _validate_key(key, required_prefix)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _delete_quietly(self, item):
        try:
            self.delete(item)
        except (IntegrityMismatch, StorageTransportError):
            pass

    def put_staging(self, source, *, expected_size, expected_sha256, staging_key=None):
        key = _validate_key(staging_key, "staging") if staging_key is not None else f"staging/{uuid.uuid4().hex}"
        with tempfile.TemporaryFile() as verified:
            size, digest = _copy_verified(source, verified, expected_size, expected_sha256)
            verified.seek(0)
            try:
                response = self.client.put_object(
                    Bucket=self.bucket,
                    Key=self._full_key(key, "staging"),
                    Body=verified,
                    ContentLength=size,
                    IfNoneMatch="*",
                    Metadata={"sha256": digest, "byte-size": str(size)},
                    **self._put_options,
                )
            except (BotoCoreError, ClientError, ParamValidationError, OSError):
                # A timed-out conditional PUT may have succeeded. Never delete
                # an ambiguously owned key here; a private staging sweeper can
                # reclaim it after its retention window.
                raise StorageTransportError() from None
        return StagedObject(
            key=key,
            sha256=digest,
            byte_size=size,
            etag=str(response.get("ETag", "")).strip('"'),
            version_id=str(response.get("VersionId", "")),
        )

    def _download_verified(self, item, target):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._full_key(item.key))
            body = response["Body"]
            try:
                size, digest = _copy_verified(body, target, item.byte_size, item.sha256)
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
            return size, digest, response
        except IntegrityMismatch:
            raise
        except ClientError as error:
            if _is_missing(error):
                raise ObjectNotFound() from None
            raise StorageTransportError() from None
        except (BotoCoreError, KeyError, OSError):
            raise StorageTransportError() from None

    def promote_immutable(self, staged, final_key):
        if not isinstance(staged, StagedObject):
            raise InvalidStorageReference()
        _validate_reference_metadata(staged)
        _validate_key(staged.key, "staging")
        _validate_key(final_key, "originals")
        with tempfile.TemporaryFile() as verified:
            self._download_verified(staged, verified)
            verified.seek(0)
            try:
                response = self.client.put_object(
                    Bucket=self.bucket,
                    Key=self._full_key(final_key, "originals"),
                    Body=verified,
                    ContentLength=staged.byte_size,
                    IfNoneMatch="*",
                    Metadata={"sha256": staged.sha256, "byte-size": str(staged.byte_size)},
                    **self._put_options,
                )
                created = True
            except ClientError as error:
                if not _is_precondition(error):
                    raise StorageTransportError() from None
                existing = ImmutableObject(final_key, staged.sha256, staged.byte_size, created=False)
                with tempfile.TemporaryFile() as sink:
                    try:
                        _, _, response = self._download_verified(existing, sink)
                    except IntegrityMismatch:
                        raise ImmutableCollision() from None
                created = False
            except (BotoCoreError, ParamValidationError, OSError):
                raise StorageTransportError() from None
        self._delete_quietly(staged)
        return ImmutableObject(
            key=final_key,
            sha256=staged.sha256,
            byte_size=staged.byte_size,
            created=created,
            etag=str(response.get("ETag", "")).strip('"'),
            version_id=str(response.get("VersionId", "")),
        )

    def open_private(self, item):
        key = _validate_key(_reference_key(item), "originals")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._full_key(key, "originals"))
            return response["Body"]
        except ClientError as error:
            if _is_missing(error):
                raise ObjectNotFound() from None
            raise StorageTransportError() from None
        except (BotoCoreError, KeyError):
            raise StorageTransportError() from None

    def delete(self, item):
        if isinstance(item, ImmutableObject) and not item.created:
            raise InvalidStorageReference()
        key = _validate_key(_reference_key(item))
        _validate_reference_metadata(item)
        if isinstance(item, str):
            self._purge_key_versions(key)
            return
        parameters = {"Bucket": self.bucket, "Key": self._full_key(key)}
        if isinstance(item, (StagedObject, ImmutableObject)):
            if item.version_id:
                parameters["VersionId"] = item.version_id
            elif item.etag:
                parameters["IfMatch"] = item.etag
        try:
            self.client.delete_object(**parameters)
        except ClientError as error:
            if _is_missing(error):
                return
            if _is_precondition(error):
                raise IntegrityMismatch() from None
            raise StorageTransportError() from None
        except (BotoCoreError, ParamValidationError):
            raise StorageTransportError() from None

    def _key_versions(self, key):
        full_key = self._full_key(key)
        parameters = {"Bucket": self.bucket, "Prefix": full_key, "MaxKeys": 1000}
        seen_cursors = set()
        versions = []
        while True:
            response = self.client.list_object_versions(**parameters)
            if not isinstance(response, dict) or type(response.get("IsTruncated")) is not bool:
                raise StorageTransportError()
            for group in ("Versions", "DeleteMarkers"):
                for version in response.get(group, ()):
                    # Prefix matching also returns adjacent object keys. Never
                    # erase those, even when they share the entire target prefix.
                    if version["Key"] == full_key:
                        version_id = version["VersionId"]
                        if not isinstance(version_id, str) or not version_id:
                            raise StorageTransportError()
                        versions.append({"Key": full_key, "VersionId": version_id})
            if not response["IsTruncated"]:
                return versions
            cursor = (response["NextKeyMarker"], response["NextVersionIdMarker"])
            if not all(isinstance(value, str) and value for value in cursor) or cursor in seen_cursors:
                raise StorageTransportError()
            seen_cursors.add(cursor)
            parameters.update(KeyMarker=cursor[0], VersionIdMarker=cursor[1])

    def _purge_key_versions(self, key):
        """Erase a logical original, including history, or leave its job retryable.

        Enumerate before deleting so pagination markers still refer to existing
        versions. Typed references retain their narrower compensation semantics.
        Immutable original keys are never reused by the application.
        """
        try:
            versions = self._key_versions(key)
            for offset in range(0, len(versions), 1000):
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": versions[offset : offset + 1000], "Quiet": True},
                )
                if response.get("Errors"):
                    raise StorageTransportError()
            # HTTP 200 can contain per-version failures; even an apparently
            # successful batch is not proof that no recoverable versions remain.
            if self._key_versions(key):
                raise StorageTransportError()
        except (BotoCoreError, ClientError, ParamValidationError, KeyError, TypeError, ValueError):
            raise StorageTransportError() from None

    def compensate_promotion(self, item):
        if not isinstance(item, ImmutableObject):
            raise InvalidStorageReference()
        if not item.created:
            return False
        self.delete(item)
        return True

    def presign_get(self, item, expires_in=300):
        expires_in = _validate_expiry(expires_in)
        key = _validate_key(_reference_key(item), "originals")
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": self._full_key(key, "originals")},
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError, ParamValidationError):
            raise StorageTransportError() from None
        return PresignedDownload(url=url, expires_at=int(self._clock()) + expires_in)
