from dataclasses import dataclass, field
import hashlib
from pathlib import Path


SUPPORTED_EXTENSIONS = frozenset({"pdf", "jpg", "jpeg", "png", "heic"})
_CHUNK_BYTES = 1024 * 1024


class SampleDiscoveryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SampleFile:
    source_hash: str
    extension: str
    byte_size: int
    path: Path = field(repr=False, compare=False)

    def public_record(self):
        return {
            "byte_size": self.byte_size,
            "extension": self.extension,
            "source_file_hash": self.source_hash,
        }


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise SampleDiscoveryError("sample_unreadable") from None
    if size <= 0:
        raise SampleDiscoveryError("sample_empty")
    return digest.hexdigest(), size


def discover_samples(root):
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, TypeError):
        raise SampleDiscoveryError("sample_root_unavailable") from None
    if not root.is_dir():
        raise SampleDiscoveryError("sample_root_unavailable")
    by_hash = {}
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold())
    except OSError:
        raise SampleDiscoveryError("sample_root_unavailable") from None
    for candidate in paths:
        extension = candidate.suffix.casefold().lstrip(".")
        if extension not in SUPPORTED_EXTENSIONS or not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        source_hash, byte_size = _digest(resolved)
        by_hash.setdefault(
            source_hash,
            SampleFile(
                source_hash=source_hash,
                extension=extension,
                byte_size=byte_size,
                path=resolved,
            ),
        )
    return tuple(by_hash[key] for key in sorted(by_hash))
