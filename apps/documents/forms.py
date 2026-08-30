from dataclasses import dataclass
import json
from pathlib import PurePosixPath

from .inspection import MAX_IMAGE_BYTES, MAX_PDF_BYTES
from .models import sanitize_display_filename


MAX_BATCH_JSON_BYTES = 64 * 1024
MAX_BATCH_FILES = 20
_EXTENSION_LIMITS = {
    "jpg": MAX_IMAGE_BYTES,
    "jpeg": MAX_IMAGE_BYTES,
    "png": MAX_IMAGE_BYTES,
    "heic": MAX_IMAGE_BYTES,
    "pdf": MAX_PDF_BYTES,
}


class BatchRequestError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReservationCandidate:
    ordinal: int
    display_filename: str
    accepted: bool
    error_code: str


def _candidate(value, ordinal):
    if not isinstance(value, dict):
        return ReservationCandidate(ordinal, f"文件-{ordinal}", False, "invalid_file_metadata")
    try:
        display_filename = sanitize_display_filename(value.get("name"))
    except (TypeError, ValueError):
        display_filename = f"文件-{ordinal}"
    byte_size = value.get("byte_size")
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        return ReservationCandidate(ordinal, display_filename, False, "invalid_file_metadata")
    extension = PurePosixPath(display_filename).suffix.casefold().lstrip(".")
    limit = _EXTENSION_LIMITS.get(extension)
    if limit is None:
        return ReservationCandidate(ordinal, display_filename, False, "unsupported_file")
    if byte_size > limit:
        return ReservationCandidate(ordinal, display_filename, False, "file_too_large")
    return ReservationCandidate(ordinal, display_filename, True, "")


def parse_batch_request(raw_body):
    if len(raw_body) > MAX_BATCH_JSON_BYTES:
        raise BatchRequestError("invalid_batch_request")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise BatchRequestError("invalid_batch_request") from None
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not files:
        raise BatchRequestError("invalid_batch_request")
    if len(files) > MAX_BATCH_FILES:
        raise BatchRequestError("batch_file_limit")
    return [_candidate(value, ordinal) for ordinal, value in enumerate(files, start=1)]
