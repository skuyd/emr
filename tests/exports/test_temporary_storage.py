from pathlib import Path

from django.test import override_settings
import pytest
import yaml

from apps.exports.files import private_temporary_file


def test_export_temporary_directory_is_used_and_closed_files_leave_no_named_copy(tmp_path):
    export_directory = tmp_path / "private-export"
    with override_settings(EXPORT_TEMP_DIRECTORY=str(export_directory)):
        with pytest.raises(FileNotFoundError):
            private_temporary_file()
        export_directory.mkdir(mode=0o700)
        with private_temporary_file() as output:
            output.write(b"synthetic-private-output")
            output.seek(0)
            assert output.read() == b"synthetic-private-output"
        assert list(export_directory.iterdir()) == []


def test_production_export_temp_storage_is_a_private_disk_volume_shared_with_downloads():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/compose.yaml").read_text(encoding="utf-8"))
    target = "/var/lib/phr/export-tmp"
    for service in ("web", "control-worker"):
        assert any(
            volume.get("type") == "volume" and volume.get("target") == target
            and volume.get("source") == "export_tmp"
            for volume in compose["services"][service]["volumes"]
        )
    assert "export_tmp" in compose["volumes"]
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")
    assert "EXPORT_TEMP_DIRECTORY=" + target in dockerfile
    assert "install -d -m 0700 -o phr -g phr " + target in dockerfile
