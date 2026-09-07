import re

from django.utils.html import strip_tags
import pytest

from apps.documents.selectors import recent_documents
from apps.labs.models import LabObservation
from apps.processing.models import DocumentSummary, DocumentType, OcrBlock, ParsingVersion
from tests.documents.test_detail_viewer import _document, _parsed_document, _patient


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("血常规检验报告单", "血常规"),
        ("检验项目：生 化 全 套", "生化"),
        ("检验项目：血常规＋生化", "血常规、生化"),
    ],
)
def test_recognized_exam_title_is_shared_by_detail_viewer_archive_and_home(django_user_model, text, expected):
    client, patient = _patient(django_user_model, text)
    document, _, _ = _parsed_document(patient)
    OcrBlock.objects.filter(parsing_version__document=document, document_page__page_number=1).update(text=text)

    detail = client.get(f"/records/{document.pk}/")
    viewer = client.get(f"/records/{document.pk}/viewer/")
    archive = client.get("/records/")
    home = client.get("/")

    assert f'<h1 id="document-title">{expected}</h1>' in detail.content.decode()
    assert f'<h1 id="viewer-title">{expected}</h1>' in viewer.content.decode()
    heading = re.search(rf'<h3\b[^>]*id="{document.pk}"[^>]*>(.*?)</h3>', archive.content.decode(), re.S)
    assert heading is not None
    title = re.sub(r'<span\b[^>]*aria-hidden="true"[^>]*>.*?</span>', "", heading[1])
    assert strip_tags(title).strip() == expected
    assert f'class="home-recent-name">{expected}</span>' in home.content.decode()
    assert document.display_filename in detail.content.decode()
    assert document.display_filename in viewer.content.decode()
    document.refresh_from_db()
    assert document.display_filename == "synthetic-report.pdf"


def test_title_can_use_reliable_indicator_categories_when_ocr_has_no_heading(django_user_model):
    client, patient = _patient(django_user_model, "indicator-title")
    document, _, _ = _parsed_document(patient)
    version = document.parsing_versions.get(active=True)
    first = version.lab_observations.get(standard_code="LAB_WBC")
    LabObservation.objects.filter(parsing_version=version, standard_code="LAB_LATER").update(
        standard_code="LAB_RBC", standard_name="红细胞计数", raw_name="红细胞", evidence=first.evidence,
        document_page=first.document_page,
    )
    assert client.get(f"/records/{document.pk}/").context["document_title"] == "血常规"

    version.lab_observations.filter(standard_code="LAB_WBC").update(standard_code="LAB_ALT")
    version.lab_observations.filter(standard_code="LAB_RBC").update(standard_code="LAB_AST")
    assert client.get(f"/records/{document.pk}/viewer/").context["document_title"] == "生化"


@pytest.mark.parametrize("narrative", [
    "建议下次复查血常规和生化",
    "建议检查项目：血常规",
    "未做检查项目：血常规",
    "建议到测试医院检查项目：血常规",
])
def test_title_ignores_uncertain_text_single_indicators_and_inactive_results(django_user_model, narrative):
    client, patient = _patient(django_user_model, "uncertain-title")
    document, _, _ = _parsed_document(patient)
    OcrBlock.objects.filter(parsing_version__document=document, document_page__page_number=1).update(
        text="生化检验报告单", confidence="0.4000",
    )
    OcrBlock.objects.filter(parsing_version__document=document, document_page__page_number=2).update(
        text=narrative,
    )
    assert client.get(f"/records/{document.pk}/").context["document_title"] == "检验报告"

    ParsingVersion.objects.filter(document=document).update(active=False)
    assert client.get(f"/records/{document.pk}/viewer/").context["document_title"] == document.display_filename


def test_title_falls_back_for_unprocessed_and_other_documents(django_user_model):
    client, patient = _patient(django_user_model, "fallback-title")
    unprocessed, _ = _document(patient)
    assert client.get(f"/records/{unprocessed.pk}/").context["document_title"] == unprocessed.display_filename
    document, _, _ = _parsed_document(patient)
    DocumentSummary.objects.filter(parsing_version__document=document).update(document_type=DocumentType.IMAGING)
    assert client.get(f"/records/{document.pk}/viewer/").context["document_title"] == "影像报告"


def test_title_evidence_is_prefetched_for_multiple_documents(django_user_model, django_assert_num_queries):
    _, patient = _patient(django_user_model, "title-queries")
    for _ in range(5):
        document, _, _ = _parsed_document(patient)
        OcrBlock.objects.filter(parsing_version__document=document).update(text="生化检验报告单")
    with django_assert_num_queries(4):
        assert [card.title for card in recent_documents(patient)] == ["生化"] * 5


@pytest.mark.parametrize(("heading", "title"), [
    ("血细胞分析", "血常规"),
    ("检验项目：血 常 规", "血常规"),
    ("", "血常规"),
    ("凝 血 四 项", "凝血功能"),
    ("检验项目：血常规＋生化", "血常规、生化"),
    ("检验项目：血常规＋生化＋肝功能＋肾功能", "血常规、生化、肝功能等检查"),
])
def test_archive_can_search_the_displayed_exam_name(django_user_model, heading, title):
    client, patient = _patient(django_user_model, "search-title")
    document, _, _ = _parsed_document(patient)
    version = document.parsing_versions.get(active=True)
    OcrBlock.objects.filter(parsing_version=version).update(text=heading or "合成检验报告")
    if not heading:
        version.lab_observations.filter(standard_code="LAB_LATER").update(standard_code="LAB_RBC")

    response = client.get("/records/", {"q": title})
    assert response.context["page_obj"].paginator.count == 1
    assert response.context["record_groups"][0].cards[0].title == title
    assert client.get("/records/", {"q": title + "、未知检查"}).context["page_obj"].paginator.count == 0

    version.ocr_blocks.update(confidence="0.2")
    version.source_evidence.update(confidence="0.2")
    assert client.get("/records/", {"q": title}).context["page_obj"].paginator.count == 0
