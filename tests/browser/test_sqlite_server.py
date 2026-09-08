"""The browser can finish Content-Length before WSGI's final database work."""
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from django.core.servers.basehttp import ThreadedWSGIServer
from django.db import connection
from django.test import TransactionTestCase

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase, SQLiteSerializedWSGIServer


class TestSQLiteStreamTeardown(SQLiteSerializedStaticLiveServerTestCase):
    def _assert_teardown_waits(self, *, block_in_close):
        if connection.vendor != "sqlite" or not connection.is_in_memory_db():
            self.skipTest("This regression requires LiveServer's shared SQLite connection")
        finalizing, release, flushing = Event(), Event(), Event()
        observed = []
        order = []
        server = self.server_thread.httpd
        original = server.get_app()

        def finish():
            finalizing.set()
            if not release.wait(10):
                raise AssertionError("The test did not release response finalization")
            order.append("final-validation")

        class Response:
            def __iter__(self):
                yield b"x"
                if not block_in_close:
                    finish()

            def close(self):
                if block_in_close:
                    finish()
                order.append("response-closed")

        def application(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "1")])
            return Response()

        def flush(_case):
            order.append("flush")
            flushing.set()

        def release_after_flush_probe():
            # A complete HTTP payload does not mean the iterator/close callback
            # has finished. Keep that tail pending while teardown attempts flush.
            observed.append(flushing.wait(0.25))
            release.set()

        waiter = Thread(target=release_after_flush_probe)
        try:
            server.set_app(application)
            with urlopen(self.live_server_url + "/synthetic-stream", timeout=10) as response:
                self.assertEqual(response.read(), b"x")
            self.assertTrue(finalizing.wait(5))
            with patch.object(TransactionTestCase, "_fixture_teardown", flush):
                waiter.start()
                self._fixture_teardown()
            waiter.join(5)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(observed, [False], "Fixture flush ran while the response was still finalizing")
            self.assertEqual(order, ["final-validation", "response-closed", "flush"])
        finally:
            release.set()
            if waiter.ident is not None:
                waiter.join(5)
            ThreadedWSGIServer.set_app(server, original)

    def test_flush_waits_for_the_last_stream_validation_after_content_length(self):
        self._assert_teardown_waits(block_in_close=False)

    def test_flush_waits_for_the_response_close_callback(self):
        self._assert_teardown_waits(block_in_close=True)

    def test_late_requests_cannot_access_the_database_during_fixture_flush(self):
        if connection.vendor != "sqlite" or not connection.is_in_memory_db():
            self.skipTest("This regression requires LiveServer's shared SQLite connection")
        calls = []
        server = self.server_thread.httpd
        original = server.get_app()

        def application(environ, start_response):
            calls.append("application")
            start_response("200 OK", [("Content-Length", "1")])
            return [b"x"]

        def flush(_case):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.live_server_url + "/late-request", timeout=5)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            self.assertEqual(calls, [])

        try:
            server.set_app(application)
            with patch.object(TransactionTestCase, "_fixture_teardown", flush):
                self._fixture_teardown()
            with urlopen(self.live_server_url + "/after-reset", timeout=5) as response:
                self.assertEqual(response.read(), b"x")
            self.assertEqual(calls, ["application"])
        finally:
            ThreadedWSGIServer.set_app(server, original)


def test_postgres_keeps_the_original_concurrent_wsgi_application():
    # No socket/DB is needed to verify that PostgreSQL isn't wrapped or gated.
    server = SQLiteSerializedWSGIServer.__new__(SQLiteSerializedWSGIServer)
    server.connections_override = {"default": SimpleNamespace(vendor="postgresql")}
    application = object()
    server.set_app(application)
    with server.database_teardown():
        assert server.get_app() is application
