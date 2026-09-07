"""Private streaming that rechecks live authorization at each output boundary."""

from django.core.exceptions import PermissionDenied


class GuardedStream:
    def __init__(self, stream, check):
        self.stream, self.check = stream, check
        self.finished = False

    def read(self, size=-1):
        if self.finished:
            return b""
        try:
            self.check()
            chunk = self.stream.read(size)
            if not chunk:
                self.finished = True
                return b""
            self.check()
            return chunk
        except PermissionDenied:
            self.close()
            return b""

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def seekable(self):
        return self.stream.seekable()

    def close(self):
        self.finished = True
        return self.stream.close()


def guarded_file_response(response):
    # WSGI sendfile bypasses read(), so do not expose the underlying descriptor.
    response.file_to_stream = None
    response.block_size = 256 * 1024
    return response
