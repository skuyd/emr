from datetime import date
from pathlib import Path

import pytest

from tests.documents.test_detail_viewer import _parsed_document, _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db
ROOT = Path(__file__).parents[2]


def test_detail_renders_original_before_automated_results_and_keeps_actions(django_user_model):
    client, patient = _patient(django_user_model, "a1")
    document, _first_evidence, _second_evidence = _parsed_document(patient)

    content = client.get(f"/records/{document.pk}/").content.decode()

    assert content.count("<h1") == 1
    assert content.index('class="detail-preview"') < content.index('class="detail-results"')
    assert 'title="原始报告预览"' in content
    assert f'href="/records/{document.pk}/original/"' in content
    assert 'target="document-preview"' in content
    assert 'name="csrfmiddlewaretoken"' in content

    viewer_template = (ROOT / "templates/documents/_viewer_panel.html").read_text(encoding="utf-8")
    assert 'aria-label="原始报告查看器"' in viewer_template
    viewer_page = (ROOT / "templates/documents/viewer.html").read_text(encoding="utf-8")
    assert "原始报告查看器" in viewer_page
    assert 'data-viewer-stage' in viewer_template and 'tabindex="0"' in viewer_template


def test_detail_and_trend_styles_contain_narrow_layout_and_focus_guards():
    detail_css = (ROOT / "static/css/detail.css").read_text(encoding="utf-8")
    trend_css = (ROOT / "static/css/trend.css").read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(0, 1.25fr) minmax(0, .95fr)" in detail_css
    assert "overflow-wrap: anywhere" in detail_css
    assert ".detail-preview iframe {" in detail_css and "width: 100%" in detail_css
    assert "min-height: 2.75rem" in detail_css
    assert "outline: 3px solid var(--color-focus)" in detail_css
    assert "@media (forced-colors: active)" in detail_css
    assert "overflow-x: auto" in trend_css
    assert "var(--color-primary)" in trend_css
    assert "var(--color-sage)" in trend_css
    assert "@media (forced-colors: active)" in trend_css


def test_trend_has_ordered_text_equivalent_and_source_links(django_user_model):
    client, patient = _patient(django_user_model, "a2")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(patient, date(2026, 8, 1), "4.6")

    content = client.get("/trends/LAB_WBC/").content.decode()

    assert content.count("<ol") >= 1
    assert 'role="img"' in content
    assert "aria-describedby=\"trend-points-1\"" in content
    assert content.count("查看来源原件") >= 2
