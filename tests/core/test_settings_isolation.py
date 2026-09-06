import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["test", "production", "build"])
def test_non_development_settings_never_load_a_local_dotenv(module):
    code = (
        "import importlib,json; from unittest.mock import patch; "
        "reader=patch('environ.Env.read_env'); mock=reader.start(); "
        f"importlib.import_module('config.settings.{module}'); "
        "print(json.dumps({'dotenv_reads':mock.call_count}))"
    )
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True, check=True)

    assert json.loads(result.stdout)["dotenv_reads"] == 0


def test_development_settings_load_dotenv_before_base_settings():
    code = """
import importlib,json,os
from unittest.mock import patch
def load(*args,**kwargs):
    os.environ['DOCUMENT_S3_PREFIX']='synthetic-dotenv-prefix'
with patch('environ.Env.read_env',side_effect=load) as reader:
    settings=importlib.import_module('config.settings.dev')
print(json.dumps({'dotenv_reads':reader.call_count,'prefix':settings.DOCUMENT_S3_PREFIX}))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == {"dotenv_reads": 1, "prefix": "synthetic-dotenv-prefix"}


def test_development_sqlite_serializes_competing_read_then_write_transactions(tmp_path):
    code = """
import json
from threading import Event, Thread
from unittest.mock import patch
import django
with patch('environ.Env.read_env'):
    django.setup()
from django.db import connection, transaction

with connection.cursor() as cursor:
    cursor.execute('CREATE TABLE synthetic_counter (value INTEGER)')
    cursor.execute('INSERT INTO synthetic_counter VALUES (0)')
connection.close()
first_read, second_started, second_read = Event(), Event(), Event()
errors = []

def increment(first):
    try:
        if not first:
            assert first_read.wait(5)
            second_started.set()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT value FROM synthetic_counter')
                value = cursor.fetchone()[0]
                if first:
                    first_read.set()
                    assert second_started.wait(5)
                    # A deferred transaction lets the competing reader in;
                    # an immediate transaction makes it wait before reading.
                    second_read.wait(0.25)
                else:
                    second_read.set()
                cursor.execute('UPDATE synthetic_counter SET value = %s', [value + 1])
    except Exception as error:
        errors.append(type(error).__name__ + ': ' + str(error))
    finally:
        connection.close()

threads = [Thread(target=increment, args=(first,)) for first in (True, False)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(10)
assert not any(thread.is_alive() for thread in threads)
with connection.cursor() as cursor:
    cursor.execute('SELECT value FROM synthetic_counter')
    value = cursor.fetchone()[0]
connection.close()
print(json.dumps({'errors': errors, 'value': value}))
"""
    environment = os.environ.copy()
    environment.update(
        PYTHONUTF8="1",
        DJANGO_SETTINGS_MODULE="config.settings.dev",
        DATABASE_URL="sqlite:///" + (tmp_path / "concurrent.sqlite3").as_posix(),
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True,
        text=True, check=True, timeout=30,
    )

    assert json.loads(result.stdout) == {"errors": [], "value": 2}
