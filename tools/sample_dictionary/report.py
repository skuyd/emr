import hashlib
import json
import os
from pathlib import Path
import tempfile

from apps.labs.candidates import LabCandidate


SCHEMA_VERSION = "1.0"


def render_candidate_json(candidates):
    candidates = tuple(sorted(candidates, key=lambda candidate: candidate._sort_key))
    if any(not isinstance(candidate, LabCandidate) for candidate in candidates):
        raise ValueError("Candidate report accepts LabCandidate values only")
    payload = {
        "candidate_count": len(candidates),
        "candidates": [candidate.public_record() for candidate in candidates],
        "schema_version": SCHEMA_VERSION,
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_candidate_report(path, candidates):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_candidate_json(candidates)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".lab-candidates-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return hashlib.sha256(payload).hexdigest()
