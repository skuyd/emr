import json

import pytest
from pypdf import PdfReader

from tools.generate_performance_fixtures import generate


def test_fixture_generator_creates_unique_three_page_nonmedical_batches_outside_repo(tmp_path):
    manifest_path = generate(tmp_path / "fixtures", batches=1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["synthetic_only"] is True
    assert len(manifest["batches"]) == 1
    batch = manifest["batches"][0]
    assert batch["total_pages"] == 60
    assert len(batch["files"]) == 20
    payloads = set()
    for item in batch["files"]:
        path = manifest_path.parent / item["name"]
        payloads.add(path.read_bytes())
        assert item["pages"] == 3
        assert len(PdfReader(path).pages) == 3
    assert len(payloads) == 20


def test_fixture_generator_never_overwrites_existing_directory(tmp_path):
    destination = tmp_path / "fixtures"
    destination.mkdir()

    with pytest.raises(FileExistsError, match="not be overwritten"):
        generate(destination, batches=1)
