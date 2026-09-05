import pytest
from django.test import Client

from apps.documents.models import DocumentStatus, ProcessingRun
from apps.labs.quality import QUALITY_POLICY_VERSION
from apps.patients.services import create_patient_space
from apps.processing.models import ParsingVersion
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrPage
from tests.processing.test_pipeline import _document_and_run, _ocr_page, _pipeline, _png_bytes, _Store


@pytest.mark.django_db
@pytest.mark.parametrize("original_only", [False, True])
def test_legacy_quality_reprocessing_publishes_current_policy_and_cannot_repeat(
    django_user_model, django_capture_on_commit_callbacks, monkeypatch, original_only
):
    document, first_run = _document_and_run(django_user_model)
    source_page = OcrPage(1, 100, 100, (), "fixture", "1.0") if original_only else _ocr_page()
    run_processing(first_run.pk, _pipeline(_Store(_png_bytes()), source_page))
    first_version = ParsingVersion.objects.get(processing_run=first_run)
    first_version.diagnostics.pop("quality_policy")
    first_version.save(update_fields=["diagnostics"])
    create_patient_space(
        document.patient.account, "合成患者",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "synthetic-quality-test"},
    )
    client = Client()
    client.force_login(document.patient.account)
    path = f"/records/{document.pk}/reprocess/"
    detail_path = f"/records/{document.pk}/"
    assert f'action="{path}"' in client.get(detail_path).content.decode()
    dispatched = []
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", dispatched.append)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(path)
    assert "retry=started" in response["Location"]
    document.refresh_from_db()
    assert document.status == DocumentStatus.PROCESSING
    assert len(dispatched) == 1
    assert "retry=unavailable" in client.post(path)["Location"]
    assert ProcessingRun.objects.filter(document=document).count() == 2

    result = run_processing(dispatched[0], _pipeline(_Store(_png_bytes()), _ocr_page()))
    assert result.state == ExecutionState.SUCCEEDED
    active = ParsingVersion.objects.get(document=document, active=True)
    assert active.diagnostics["quality_policy"] == QUALITY_POLICY_VERSION
    assert active.pk != first_version.pk
    assert f'action="{path}"' not in client.get(detail_path).content.decode()
    assert "retry=unavailable" in client.post(path)["Location"]
    assert ProcessingRun.objects.filter(document=document).count() == 2
