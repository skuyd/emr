from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.servers.basehttp import ThreadedWSGIServer
from django.db import connection, transaction

from apps.glucose.models import GlucoseRecord
from apps.glucose.services import create_record
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase, SQLiteSerializedWSGIServer
from tests.documents.test_detail_viewer import _patient
from tests.glucose.test_forms import values


class TestSQLiteDatabaseActions(SQLiteSerializedStaticLiveServerTestCase):
    def test_database_observation_waits_for_shared_sqlite_response_finalization(self):
        self.assertEqual(connection.vendor, 'sqlite')
        self.assertTrue(connection.is_in_memory_db())
        _, patient = _patient(get_user_model(), 'synthetic-pending-response')
        record = create_record(patient, patient.account, values(value='7.1'), creation_key=uuid4()).record
        server = self.server_thread.httpd
        original = server.get_app()
        pending, release, finalized, observed = Event(), Event(), Event(), Event()
        result, failures, premature = [], [], []

        def application(environ, start_response):
            try:
                with transaction.atomic():
                    GlucoseRecord.objects.filter(pk=record.pk).update(current_data=record.current_data)
                    start_response('200 OK', [('Content-Length', '1')])
                    yield b'x'
                    pending.set()
                    if not release.wait(10):
                        raise AssertionError('Synthetic finalization not released')
            finally:
                finalized.set()

        def observe_database():
            try:
                result.append(self.database_action(lambda: GlucoseRecord.objects.get(pk=record.pk).current_data['raw_value']))
            except Exception as error:
                failures.append((type(error).__name__, str(error)))
            finally:
                observed.set()

        worker = Thread(target=observe_database)
        try:
            server.set_app(application)
            with urlopen(self.live_server_url + '/synthetic-pending-response', timeout=10) as response:
                self.assertEqual(response.read(), b'x')
            self.assertTrue(pending.wait(5))
            worker.start()
            premature.append(observed.wait(.25))
            release.set()
            worker.join(5)
            self.assertTrue(finalized.wait(5))
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [], 'Test ORM raced a response transaction: ' + repr(failures))
            self.assertEqual(premature, [False])
            self.assertEqual(result, ['7.1'])
            self.assertFalse(connection.in_atomic_block)
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(5)
            finalized.wait(5)
            ThreadedWSGIServer.set_app(server, original)


def test_database_actions_keep_postgres_server_observers_concurrent():
    server = SQLiteSerializedWSGIServer.__new__(SQLiteSerializedWSGIServer)
    server.connections_override = {'default': SimpleNamespace(vendor='postgresql')}
    server.set_app(object())
    case = SimpleNamespace(server_thread=SimpleNamespace(httpd=server))
    both_observers = Barrier(2)

    def observe(value):
        both_observers.wait(timeout=5)
        return value

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(SQLiteSerializedStaticLiveServerTestCase.database_action,
                                  case, lambda value=value: observe(value)) for value in (1, 2)]
        assert [result.result(timeout=10) for result in results] == [1, 2]
