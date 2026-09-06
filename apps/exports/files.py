"""An owned, seekable artifact. Large outputs stay in private temporary files."""

import hashlib
import io
import tempfile


CHUNK_SIZE = 64 * 1024


def private_temporary_file():
    # TemporaryFile uses exclusive, private creation and removes the file on close.
    return tempfile.TemporaryFile(mode="w+b")


class Artifact:
    def __init__(self, payload, filename, content_type):
        self.stream = io.BytesIO(payload)
        self.filename, self.content_type = filename, content_type
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self.byte_size = len(payload)

    @classmethod
    def from_stream(cls, stream, filename, content_type, *, sha256=None, byte_size=None):
        result = cls.__new__(cls)
        result.stream, result.filename, result.content_type = stream, filename, content_type
        if sha256 is None or byte_size is None:
            stream.seek(0)
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
            sha256, byte_size = digest.hexdigest(), size
        result.sha256, result.byte_size = sha256, byte_size
        stream.seek(0)
        return result

    @property
    def payload(self):
        """Convenience for small consumers/tests; generation and HTTP use stream."""
        position = self.stream.tell()
        try:
            self.stream.seek(0)
            return self.stream.read()
        finally:
            self.stream.seek(position)

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
