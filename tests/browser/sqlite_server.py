"""Serialize only LiveServer's shared in-memory SQLite WSGI connection.

PostgreSQL retains the standard concurrent server, so browser runs against the
required PostgreSQL settings exercise independent connections and real locks.
"""

from threading import Lock

from django.core.servers.basehttp import ThreadedWSGIServer
from django.test.testcases import LiveServerThread


class SQLiteSerializedWSGIServer(ThreadedWSGIServer):
    def set_app(self, application):
        shared_sqlite = any(
            conn.vendor == "sqlite" and conn.is_in_memory_db()
            for conn in (self.connections_override or {}).values()
        )
        if not shared_sqlite:
            return super().set_app(application)
        request_lock = Lock()

        def serialized_application(environ, start_response):
            # Lock WSGI work and iteration, not the socket: idle HTTP keepalive
            # connections must not prevent another request from being served.
            with request_lock:
                result = application(environ, start_response)
                try:
                    yield from result
                finally:
                    if hasattr(result, "close"):
                        result.close()

        return super().set_app(serialized_application)


class SQLiteSerializedLiveServerThread(LiveServerThread):
    server_class = SQLiteSerializedWSGIServer
