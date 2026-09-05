import hashlib
import io
import threading
import time
import uuid

from botocore.exceptions import ClientError, ReadTimeoutError

from apps.documents.errors import (
    ImmutableCollision,
    IntegrityMismatch,
    InvalidStorageReference,
    ObjectNotFound,
    StagingAccessDenied,
    StorageTransportError,
)
from apps.documents.storage import ImmutableObject, PresignedDownload, StagedObject


class InMemoryObjectStore:
    """Strict private store fake shared by upload-service tests."""

    def __init__(self):
        self.objects = {}
        self._lock = threading.Lock()
        self.fail_put = False
        self.fail_promote = False
        self.fail_delete = False
        self.calls = []

    def put_staging(self, source, *, expected_size, expected_sha256):
        self.calls.append(("put_staging", expected_size))
        if self.fail_put:
            raise StorageTransportError()
        payload = source.read(expected_size + 1)
        if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise IntegrityMismatch()
        key = f"staging/{uuid.uuid4().hex}"
        with self._lock:
            self.objects[key] = payload
        return StagedObject(key, expected_sha256, expected_size)

    def promote_immutable(self, staged, final_key):
        self.calls.append(("promote_immutable", staged.key, final_key))
        if self.fail_promote:
            raise StorageTransportError()
        with self._lock:
            payload = self.objects[staged.key]
            existing = self.objects.get(final_key)
            if existing is not None and existing != payload:
                raise ImmutableCollision()
            created = existing is None
            self.objects.setdefault(final_key, payload)
            del self.objects[staged.key]
        return ImmutableObject(final_key, staged.sha256, staged.byte_size, created=created)

    def open_private(self, item):
        key = item.key if hasattr(item, "key") else item
        if key.startswith("staging/"):
            raise StagingAccessDenied()
        try:
            payload = self.objects[key]
        except KeyError:
            raise ObjectNotFound() from None
        return io.BytesIO(payload)

    def delete(self, item):
        if isinstance(item, ImmutableObject) and not item.created:
            raise InvalidStorageReference()
        key = item.key if hasattr(item, "key") else item
        self.calls.append(("delete", key))
        if self.fail_delete:
            raise StorageTransportError()
        with self._lock:
            payload = self.objects.get(key)
            if payload is not None and hasattr(item, "sha256") and hashlib.sha256(payload).hexdigest() != item.sha256:
                raise IntegrityMismatch()
            self.objects.pop(key, None)

    def compensate_promotion(self, item):
        if not isinstance(item, ImmutableObject):
            raise InvalidStorageReference()
        if not item.created:
            return False
        self.delete(item)
        return True

    def presign_get(self, item, expires_in=300):
        key = item.key if hasattr(item, "key") else item
        if key.startswith("staging/"):
            raise StagingAccessDenied()
        return PresignedDownload(url=f"/test-private/{uuid.uuid4().hex}", expires_at=int(time.time()) + expires_in)


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.failures = {}
        self.lose_next_put_response = False
        self._version = 0

    def fail_once(self, operation, code="ServiceUnavailable", status=503):
        self.failures[operation] = (code, status)

    def write_then_lose_put_response_once(self):
        self.lose_next_put_response = True

    def _maybe_fail(self, operation):
        failure = self.failures.pop(operation, None)
        if failure:
            code, status = failure
            raise ClientError(
                {"Error": {"Code": code, "Message": "safe fake failure"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                operation,
            )

    def put_object(self, **parameters):
        self._maybe_fail("put_object")
        body = parameters.pop("Body")
        payload = body.read()
        key = parameters["Key"]
        self.calls.append(("put_object", dict(parameters)))
        if parameters.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "exists"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self._version += 1
        etag = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        version_id = str(self._version)
        self.objects[key] = {
            "body": payload,
            "metadata": parameters.get("Metadata", {}),
            "etag": etag,
            "version_id": version_id,
        }
        if self.lose_next_put_response:
            self.lose_next_put_response = False
            raise ReadTimeoutError(endpoint_url="https://objects.invalid")
        return {"ETag": f'"{etag}"', "VersionId": version_id}

    def get_object(self, **parameters):
        self._maybe_fail("get_object")
        self.calls.append(("get_object", dict(parameters)))
        stored = self.objects.get(parameters["Key"])
        if stored is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        return {
            "Body": io.BytesIO(stored["body"]),
            "ETag": f'"{stored["etag"]}"',
            "VersionId": stored["version_id"],
            "Metadata": stored["metadata"],
        }

    def delete_object(self, **parameters):
        self._maybe_fail("delete_object")
        self.calls.append(("delete_object", dict(parameters)))
        stored = self.objects.get(parameters["Key"])
        if stored is not None and parameters.get("IfMatch") not in {None, stored["etag"]}:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "changed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "DeleteObject",
            )
        if stored is not None and parameters.get("VersionId") not in {None, stored["version_id"]}:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing version"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "DeleteObject",
            )
        self.objects.pop(parameters["Key"], None)
        return {}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self._maybe_fail("generate_presigned_url")
        self.calls.append(("generate_presigned_url", operation, dict(Params), ExpiresIn))
        return f"https://objects.invalid/private-token?expires={ExpiresIn}"


class VersionedS3Client(FakeS3Client):
    """Versioned bucket: key-only DELETE hides bytes without erasing history."""

    def __init__(self, *, page_size=2):
        super().__init__()
        self.versions = {}
        self.page_size = page_size
        self.failed_version_ids = set()
        self.retain_deleted_versions = False

    def add_version(self, key, *, marker=False):
        self._version += 1
        version_id = str(self._version)
        self.versions[(key, version_id)] = marker
        return version_id

    def delete_object(self, **parameters):
        self._maybe_fail("delete_object")
        self.calls.append(("delete_object", dict(parameters)))
        key = parameters["Key"]
        if "VersionId" in parameters:
            self.versions.pop((key, parameters["VersionId"]), None)
        else:
            self.add_version(key, marker=True)
        return {}

    def list_object_versions(self, **parameters):
        self._maybe_fail("list_object_versions")
        self.calls.append(("list_object_versions", dict(parameters)))
        identities = sorted(identity for identity in self.versions if identity[0].startswith(parameters["Prefix"]))
        if "KeyMarker" in parameters:
            cursor = (parameters["KeyMarker"], parameters.get("VersionIdMarker", ""))
            identities = [identity for identity in identities if identity > cursor]
        page = identities[: min(self.page_size, parameters.get("MaxKeys", 1000))]
        response = {"IsTruncated": len(identities) > len(page), "Versions": [], "DeleteMarkers": []}
        for key, version_id in page:
            group = "DeleteMarkers" if self.versions[(key, version_id)] else "Versions"
            response[group].append({"Key": key, "VersionId": version_id})
        if response["IsTruncated"]:
            response.update(NextKeyMarker=page[-1][0], NextVersionIdMarker=page[-1][1])
        return response

    def delete_objects(self, **parameters):
        self._maybe_fail("delete_objects")
        assert 1 <= len(parameters["Delete"]["Objects"]) <= 1000
        self.calls.append(("delete_objects", parameters))
        deleted, errors = [], []
        for identity in parameters["Delete"]["Objects"]:
            if identity["VersionId"] in self.failed_version_ids:
                errors.append({**identity, "Code": "AccessDenied", "Message": "synthetic retention"})
            else:
                if not self.retain_deleted_versions:
                    self.versions.pop((identity["Key"], identity["VersionId"]), None)
                deleted.append(identity)
        return {"Deleted": deleted, "Errors": errors}
