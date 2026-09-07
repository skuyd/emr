from datetime import date
import io
import hashlib
import uuid

from django.test import Client
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter
import pytest

from apps.documents.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    InaccuracyFeedback,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
)
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.patients.services import create_patient_space
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    OcrBlock,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "detail-viewer-test"}


def test_detail_groups_repeated_quality_reasons_by_affected_observations(django_user_model):
    client, patient = _patient(django_user_model, "quality-summary")
    document, _, _ = _parsed_document(patient)
    rows = LabObservation.objects.filter(parsing_version__document=document)
    rows.update(quality_issues=[
        {"code": "association_conflict", "rule_id": "value-layout", "label": "字段关联冲突", "fields": ["raw_value"]},
        {"code": "association_conflict", "rule_id": "name-layout", "label": "字段关联冲突", "fields": ["raw_name"]},
    ])
    response = client.get(f"/records/{document.pk}/")
    assert response.status_code == 200
    summary = {item["code"]: item for item in response.context["quality_summary"]}
    assert summary["association_conflict"]["count"] == 2
    assert summary["association_conflict"]["description"]
    assert response.context["quality_observation_count"] == 2
    assert all(len({item["code"] for item in row.display_issues}) == len(row.display_issues)
               for row in response.context["observations"])


def _patient(django_user_model, marker):
    account = django_user_model.objects.create(
        phone_hash=hashlib.sha256(marker.encode()).hexdigest(), phone_encrypted="ciphertext"
    )
    patient = create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client = Client()
    client.force_login(account)
    return client, patient


def _document(patient, *, content_type="application/pdf", page_count=2, status=DocumentStatus.ORGANIZED):
    marker = uuid.uuid4().hex
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=page_count, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="synthetic-report.pdf" if content_type == "application/pdf" else "synthetic-image.png",
        content_type=content_type,
        byte_size=128,
        page_count=page_count,
        sha256=marker * 2,
        original_object_key=f"originals/{marker}",
        status=status,
    )
    pages = tuple(
        DocumentPage.objects.create(document=document, page_number=number, width=1000, height=1400)
        for number in range(1, page_count + 1)
    )
    return document, pages


def _parsed_document(patient):
    document, pages = _document(patient)
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
        stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(),
        is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=document,
        processing_run=run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        dictionary_version="1.0.0",
        dictionary_hash="a" * 64,
        status=ParsingVersionStatus.READY,
    )
    ParsingVersion.objects.activate(version)
    DocumentSummary.objects.create(
        parsing_version=version,
        document_type=DocumentType.LAB,
        document_date_raw="2026-08-20",
        document_date=date(2026, 8, 20),
        date_precision=DatePrecision.DAY,
        institution_raw="合成检验中心",
        confidence="0.9000",
    )
    OcrBlock.objects.create(
        parsing_version=version,
        document_page=pages[0],
        reading_order=1,
        text="第一页 OCR 原始文字",
        polygon=((0.1, 0.1), (0.8, 0.1), (0.8, 0.2), (0.1, 0.2)),
        confidence="0.9800",
    )
    OcrBlock.objects.create(
        parsing_version=version,
        document_page=pages[1],
        reading_order=1,
        text="第二页 OCR 原始文字",
        polygon=((0.1, 0.1), (0.8, 0.1), (0.8, 0.2), (0.1, 0.2)),
        confidence="0.9800",
    )
    first_evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[0],
        polygon=((0.1, 0.2), (0.6, 0.2), (0.6, 0.3), (0.1, 0.3)),
        source_text="原始白细胞 <4.20 10^9/L",
        confidence="0.9800",
    )
    second_evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[1],
        polygon=None,
        source_text="后页项目 阳性",
        confidence="0.9200",
    )
    LabObservation.objects.create(
        parsing_version=version,
        document_page=pages[1],
        evidence=second_evidence,
        reading_order=1,
        raw_name="后页项目",
        standard_code="LAB_LATER",
        standard_name="后页项目",
        raw_value="阳性",
        result_type=ResultType.QUALITATIVE,
        raw_unit="",
        capability_level=CapabilityLevel.STABLE,
        dictionary_version="1.0.0",
    )
    LabObservation.objects.create(
        parsing_version=version,
        document_page=pages[0],
        evidence=first_evidence,
        reading_order=2,
        raw_name="原始白细胞",
        standard_code="LAB_WBC",
        standard_name="白细胞计数",
        raw_value="<4.20",
        result_type=ResultType.COMPARATOR,
        raw_unit="10^9/L",
        reference_range_raw="3.50—9.50",
        report_flag_raw="L",
        capability_level=CapabilityLevel.STABLE,
        dictionary_version="1.0.0",
    )
    return document, first_evidence, second_evidence


def _pdf_bytes(page_count=2):
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=300, height=400)
    writer.write(output)
    return output.getvalue()


def _png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (80, 60), "#718096").save(output, format="PNG")
    return output.getvalue()


def test_detail_preserves_raw_results_orders_by_report_and_keeps_source_links(django_user_model):
    client, patient = _patient(django_user_model, "g")
    document, first_evidence, _second_evidence = _parsed_document(patient)

    response = client.get(f"/records/{document.pk}/")
    content = response.content.decode()

    assert response.status_code == 200
    for expected in (
        "检验报告",
        "2026年8月20日",
        "合成检验中心",
        "已整理",
        "2 页",
        "原始白细胞",
        "白细胞计数",
        "&lt;4.20",
        "10^9/L",
        "3.50—9.50",
        "报告标记",
        "自动整理结果可能不准确，请以原始报告为准。这里只帮助查找，不提供诊断或治疗建议。",
        "第一页 OCR 原始文字",
    ):
        assert expected in content
    assert content.index("原始白细胞") < content.index("后页项目")
    assert f"evidence={first_evidence.pk}" in content
    assert 'target="document-preview"' in content
    assert f'/records/{document.pk}/viewer/?embed=1' in content
    assert "confidence" not in content.casefold()
    assert "STABLE" not in content
    assert response["Cache-Control"] == "private, no-store, max-age=0"


def test_detail_is_original_first_with_fixed_trust_note_and_download(django_user_model):
    client, patient = _patient(django_user_model, "g2")
    document, _first_evidence, _second_evidence = _parsed_document(patient)

    response = client.get(f"/records/{document.pk}/")
    content = response.content.decode()

    assert response.status_code == 200
    assert content.index('<h2 id="preview-title">原始报告</h2>') < content.index('<h2 id="results-title">自动整理结果</h2>')
    assert (
        "自动整理结果可能不准确，请以原始报告为准。这里只帮助查找，不提供诊断或治疗建议。"
        in content
    )
    assert f'href="/records/{document.pk}/original/"' in content


@pytest.mark.parametrize(
    ("status", "status_key"),
    (
        (DocumentStatus.PROCESSING, "processing"),
        (DocumentStatus.ORGANIZED, "organized"),
        (DocumentStatus.ORIGINAL_ONLY, "original"),
        (DocumentStatus.PROCESSING_FAILED, "failed"),
    ),
)
def test_detail_status_uses_shared_icon_text_and_color_badge(django_user_model, status, status_key):
    client, patient = _patient(django_user_model, f"badge-{status_key}")
    document, _pages = _document(patient, status=status)

    response = client.get(f"/records/{document.pk}/")
    content = response.content.decode()

    assert response.status_code == 200
    status_row = content.split("处理状态", 1)[1].split("</div>", 1)[0]
    assert f'class="status-badge status-badge--{status_key}"' in status_row
    assert 'class="status-badge__icon" aria-hidden="true"' in status_row
    assert 'class="status-badge__label"' in status_row


def test_viewer_uses_scoped_evidence_to_select_page_and_highlight(django_user_model):
    client, patient = _patient(django_user_model, "h")
    document, first_evidence, second_evidence = _parsed_document(patient)

    highlighted = client.get(f"/records/{document.pk}/viewer/", {"evidence": first_evidence.pk})
    page_only = client.get(f"/records/{document.pk}/viewer/", {"evidence": second_evidence.pk})
    invalid = client.get(f"/records/{document.pk}/viewer/", {"page": 2, "evidence": "not-a-uuid"})

    content = highlighted.content.decode()
    assert highlighted.status_code == 200
    assert 'data-initial-page="1"' in content
    assert 'data-highlight-page="1"' in content
    assert 'data-highlight-rect="10.0000,20.0000,50.0000,10.0000"' in content
    assert "原始白细胞 <4.20" not in content
    assert "下载" not in content and "分享" not in content
    assert 'data-viewer-thumbnail="2"' in content
    assert page_only.status_code == 200 and 'data-initial-page="2"' in page_only.content.decode()
    assert 'data-highlight-page="0"' in page_only.content.decode()
    assert invalid.status_code == 200 and 'data-initial-page="2"' in invalid.content.decode()
    assert "frame-ancestors 'none'" in highlighted["Content-Security-Policy"]


def test_embed_viewer_is_same_origin_frameable_and_has_no_app_shell(django_user_model):
    client, patient = _patient(django_user_model, "i")
    document, _pages = _document(patient)

    response = client.get(f"/records/{document.pk}/viewer/", {"embed": 1})
    content = response.content.decode()

    assert response.status_code == 200
    assert "viewer-embed-body" in content
    assert "app-sidebar" not in content
    assert "frame-ancestors 'self'" in response["Content-Security-Policy"]
    assert response["X-Frame-Options"] == "SAMEORIGIN"


def test_pdf_and_image_page_endpoints_render_private_pngs(django_user_model, monkeypatch):
    client, patient = _patient(django_user_model, "j")
    pdf, _pdf_pages = _document(patient)
    image, _image_pages = _document(patient, content_type="image/png", page_count=1)
    store = InMemoryObjectStore()
    store.objects[pdf.original_object_key] = _pdf_bytes()
    store.objects[image.original_object_key] = _png_bytes()
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)

    pdf_response = client.get(f"/records/{pdf.pk}/pages/2/image/")
    image_response = client.get(f"/records/{image.pk}/pages/1/image/", {"thumbnail": 1})
    sheet_response = client.get(f"/records/{pdf.pk}/thumbnails/sheet/")

    assert pdf_response.status_code == 200
    assert image_response.status_code == 200
    for response in (pdf_response, image_response, sheet_response):
        assert response["Content-Type"] == "image/png"
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert response["Cache-Control"] == "private, no-store, max-age=0"
        assert "Content-Disposition" not in response
    rendered = Image.open(io.BytesIO(image_response.content))
    assert rendered.width <= 160 and rendered.height <= 220
    sheet = Image.open(io.BytesIO(sheet_response.content))
    assert sheet.size == (160, 440)
    assert client.get(f"/records/{pdf.pk}/pages/3/image/").status_code == 404


@pytest.mark.parametrize(
    ("content_type", "filename", "payload"),
    [("application/pdf", "original.pdf", _pdf_bytes), ("image/png", "original.png", _png_bytes)],
)
def test_original_download_is_an_attachment_with_private_exact_bytes(
    django_user_model, monkeypatch, content_type, filename, payload
):
    client, patient = _patient(django_user_model, "d")
    document, _pages = _document(patient, content_type=content_type, page_count=1)
    original = payload()
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = original
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)

    response = client.get(f"/records/{document.pk}/original/")

    assert response.status_code == 200
    assert response["Content-Disposition"] == f'attachment; filename="{filename}"'
    assert response["Cache-Control"] == "private, no-store, max-age=0"
    assert b"".join(response.streaming_content) == original


def test_detail_viewer_and_page_image_never_cross_patient_or_show_deleted(django_user_model, monkeypatch):
    owner_client, owner = _patient(django_user_model, "k")
    other_client, _other = _patient(django_user_model, "l")
    document, _pages = _document(owner, content_type="image/png", page_count=1)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = _png_bytes()
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)

    for path in (
        f"/records/{document.pk}/",
        f"/records/{document.pk}/viewer/",
        f"/records/{document.pk}/pages/1/image/",
    ):
        assert other_client.get(path).status_code == 404
        assert owner_client.get(path).status_code == 200

    Document.objects.filter(pk=document.pk).update(deleted_at=timezone.now())
    for path in (
        f"/records/{document.pk}/",
        f"/records/{document.pk}/viewer/",
        f"/records/{document.pk}/pages/1/image/",
    ):
        assert owner_client.get(path).status_code == 404


def test_viewer_javascript_exposes_required_mouse_keyboard_rotation_drag_and_fullscreen_controls():
    javascript = open("static/js/viewer.js", encoding="utf-8").read()

    for contract in (
        'addEventListener("wheel"',
        'addEventListener("keydown"',
        'addEventListener("pointerdown"',
        "requestFullscreen",
        "state.rotation",
        'event.key === "ArrowLeft"',
        'event.key === "ArrowRight"',
    ):
        assert contract in javascript


def test_inaccuracy_feedback_is_one_click_idempotent_and_contains_no_medical_text(django_user_model):
    client, patient = _patient(django_user_model, "m")
    document, _first_evidence, _second_evidence = _parsed_document(patient)
    path = f"/records/{document.pk}/feedback/"

    first = client.post(path)
    second = client.post(path)

    assert first.status_code == 302 and "feedback=thanks" in first["Location"]
    assert second.status_code == 302
    assert InaccuracyFeedback.objects.filter(document=document).count() == 1
    feedback = InaccuracyFeedback.objects.get(document=document)
    assert feedback.parsing_version.active is True
    assert feedback.category == "DOCUMENT_RECOGNITION"
    field_names = {field.name for field in InaccuracyFeedback._meta.fields}
    assert field_names.isdisjoint({"correct_value", "raw_text", "comment", "medical_text"})
    assert client.get(path).status_code == 405
    confirmation = client.get(first["Location"]).content.decode()
    assert "已收到反馈" in confirmation
    assert "无需填写正确答案" in confirmation


def test_failed_document_can_queue_exactly_one_patient_scoped_reprocessing_run(
    django_user_model, monkeypatch, django_capture_on_commit_callbacks
):
    client, patient = _patient(django_user_model, "n")
    other_client, _other = _patient(django_user_model, "o")
    document, _pages = _document(patient, status=DocumentStatus.PROCESSING_FAILED)
    ProcessingRun.objects.create(
        document=document,
        parser_version="phr-v1",
        task_type="INITIAL_PARSE",
        idempotency_key=f"{document.pk}:phr-v1:INITIAL_PARSE",
        attempt_number=1,
        stage=ProcessingStage.FAILED,
        error_code="synthetic_failure",
        finished_at=timezone.now(),
    )
    dispatched = []
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", lambda run_id: dispatched.append(run_id))
    path = f"/records/{document.pk}/reprocess/"

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(path)
    repeated = client.post(path)

    assert response.status_code == 302 and "retry=started" in response["Location"]
    assert repeated.status_code == 302 and "retry=unavailable" in repeated["Location"]
    document.refresh_from_db()
    assert document.status == DocumentStatus.PROCESSING
    retry = document.processing_runs.get(attempt_number=2)
    assert retry.stage == ProcessingStage.QUEUED
    assert retry.task_type == "USER_RETRY_2"
    assert dispatched == [retry.pk]
    assert other_client.post(path).status_code == 404
    assert client.get(path).status_code == 405
