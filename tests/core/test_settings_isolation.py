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
