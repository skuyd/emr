import re

from .models import Document


_SHA256 = re.compile(r"[0-9a-f]{64}")


class InvalidDuplicateDigest(ValueError):
    pass


def find_exact_duplicate(patient, sha256, *, lock=False):
    """Return only this patient's active exact match, or ``None``.

    The patient predicate is part of the SQL query so callers cannot turn this
    helper into a cross-account existence oracle.
    """

    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise InvalidDuplicateDigest("invalid_duplicate_digest")
    query = Document.objects.filter(patient_id=patient.pk, sha256=sha256, deleted_at__isnull=True)
    if lock:
        query = query.select_for_update()
    return query.order_by("created_at", "pk").first()
