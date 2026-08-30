class UploadDomainError(Exception):
    """An upload error whose string form is safe to return or log."""

    code = "upload_error"

    def __init__(self, code=None):
        if code is not None:
            self.code = code
        super().__init__(self.code)

    def __str__(self):
        return self.code


class InspectionError(UploadDomainError):
    """The exact uploaded artifact failed a server-side content check."""


class ArtifactClosed(UploadDomainError):
    code = "artifact_closed"


class InvalidStorageReference(UploadDomainError):
    code = "invalid_storage_reference"


class IntegrityMismatch(UploadDomainError):
    code = "integrity_mismatch"


class StagingAccessDenied(UploadDomainError):
    code = "staging_access_denied"


class ImmutableCollision(UploadDomainError):
    code = "immutable_collision"


class ObjectNotFound(UploadDomainError):
    code = "object_not_found"


class InvalidPresignExpiry(UploadDomainError):
    code = "invalid_presign_expiry"


class StorageTransportError(UploadDomainError):
    code = "storage_transport_error"
