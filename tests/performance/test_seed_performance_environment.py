import json

from django.core.management import call_command, CommandError
from django.test import override_settings
import pytest

from apps.accounts.models import AccountSession
from apps.documents.models import Document, DocumentPage
from apps.labs.models import LabObservation
from apps.patients.models import Patient
from apps.processing.models import OcrBlock


CONFIRMATION = "SYNTHETIC-PERFORMANCE-DATA"


def command_arguments(tmp_path):
    return [
        "--confirm",
        CONFIRMATION,
        "--namespace",
        "pytest-release",
        "--base-url",
        "https://phr.example.test",
        "--session-output",
        str(tmp_path / "sessions.json"),
        "--browser-state-output",
        str(tmp_path / "browser.json"),
        "--onboarding-state-output",
        str(tmp_path / "onboarding.json"),
        "--accounts",
        "2",
        "--documents-per-account",
        "3",
        "--pages-per-document",
        "1",
        "--characters-per-page",
        "64",
    ]


@pytest.mark.django_db
def test_performance_seed_is_disabled_by_default(tmp_path):
    with pytest.raises(CommandError, match="disabled"):
        call_command("seed_performance_environment", *command_arguments(tmp_path))


@pytest.mark.django_db
def test_performance_seed_builds_only_synthetic_data_and_private_session_artifacts(tmp_path):
    object_root = tmp_path / "objects"
    with override_settings(
        ALLOW_PERFORMANCE_SEED=True,
        DOCUMENT_STORAGE_BACKEND="local",
        DOCUMENT_STORAGE_ROOT=object_root,
    ):
        call_command("seed_performance_environment", *command_arguments(tmp_path))

    assert Patient.objects.count() == 2
    assert Document.objects.count() == 6
    assert DocumentPage.objects.count() == 6
    assert OcrBlock.objects.count() == 6
    assert LabObservation.objects.count() == 4
    assert AccountSession.objects.count() == 3
    assert len(list((object_root / "originals" / "performance" / "pytest-release").rglob("*.pdf"))) == 2

    session_data = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert session_data["synthetic_only"] is True
    assert session_data["design"] == {
        "accounts": 2,
        "documents_per_account": 3,
        "pages_per_document": 1,
        "characters_per_page": 64,
    }
    assert len(session_data["sessions"]) == 2
    assert all(row["document_count"] == 3 for row in session_data["sessions"])
    assert all(row["sessionid"] for row in session_data["sessions"])
    assert json.loads((tmp_path / "browser.json").read_text(encoding="utf-8"))["cookies"][0]["secure"] is True
    assert json.loads((tmp_path / "onboarding.json").read_text(encoding="utf-8"))["cookies"][0]["secure"] is True


@pytest.mark.django_db
def test_performance_seed_never_overwrites_credential_outputs(tmp_path):
    arguments = command_arguments(tmp_path)
    (tmp_path / "sessions.json").write_text("preserved\n", encoding="utf-8")
    with override_settings(ALLOW_PERFORMANCE_SEED=True):
        with pytest.raises(CommandError, match="never overwritten"):
            call_command("seed_performance_environment", *arguments)
    assert (tmp_path / "sessions.json").read_text(encoding="utf-8") == "preserved\n"
