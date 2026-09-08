"""Serialize only LiveServer's shared in-memory SQLite WSGI connection.

PostgreSQL retains the standard concurrent server, so browser runs against the
required PostgreSQL settings exercise independent connections and real locks.
"""

from contextlib import contextmanager
from threading import Condition, Lock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.servers.basehttp import ThreadedWSGIServer
from django.test.testcases import LiveServerThread


class SQLiteSerializedWSGIServer(ThreadedWSGIServer):
    def set_app(self, application):
        shared_sqlite = any(
            conn.vendor == "sqlite" and conn.is_in_memory_db()
            for conn in (self.connections_override or {}).values()
        )
        if not shared_sqlite:
            self._sqlite_requests = None
            return super().set_app(application)
        if getattr(self, "_sqlite_requests", None) is None:
            self._sqlite_requests = Condition()
            self._sqlite_request_lock = Lock()
            self._sqlite_active = 0
            self._sqlite_tearing_down = False

        def serialized_application(environ, start_response):
            with self._sqlite_requests:
                accepted = not self._sqlite_tearing_down
                if accepted:
                    self._sqlite_active += 1
            if not accepted:
                # A late browser callback must not enter an empty/half-reset
                # test database. No application code or audit runs here.
                body = b"Test database is resetting"
                start_response("503 Service Unavailable", [("Content-Length", str(len(body)))])
                yield body
                return
            # Lock WSGI work and iteration, not the socket: idle HTTP keepalive
            # connections must not prevent another request from being served.
            try:
                with self._sqlite_request_lock:
                    result = application(environ, start_response)
                    try:
                        yield from result
                    finally:
                        if hasattr(result, "close"):
                            result.close()
            finally:
                with self._sqlite_requests:
                    self._sqlite_active -= 1
                    self._sqlite_requests.notify_all()

        return super().set_app(serialized_application)

    @contextmanager
    def database_teardown(self):
        requests = getattr(self, "_sqlite_requests", None)
        if requests is None:
            yield
            return
        with requests:
            self._sqlite_tearing_down = True
        try:
            with requests:
                if not requests.wait_for(lambda: self._sqlite_active == 0, timeout=10):
                    raise AssertionError("LiveServer response did not finish before SQLite fixture teardown")
            yield
        finally:
            with requests:
                self._sqlite_tearing_down = False


class SQLiteSerializedLiveServerThread(LiveServerThread):
    server_class = SQLiteSerializedWSGIServer


class SQLiteSerializedStaticLiveServerTestCase(StaticLiveServerTestCase):
    server_thread_class = SQLiteSerializedLiveServerThread

    def _fixture_teardown(self):
        # Content-Length can complete the browser download before WSGI's final
        # source recheck and audit. Drain admitted requests before DB flush.
        with self.server_thread.httpd.database_teardown():
            super()._fixture_teardown()
