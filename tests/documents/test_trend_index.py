from datetime import date
import re

import pytest
from django.urls import reverse

from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_trend_index_lists_only_eligible_current_patient_summaries(django_user_model):
    client, patient = _patient(django_user_model, "index-owner")
    _observation(patient, date(2026, 7, 1), "4.200")
    _observation(patient, date(2026, 8, 20), "5.0")
    _observation(patient, date(2026, 8, 21), "88", code="LAB_SINGLE", standard_name="单次指标")

    response = client.get(reverse("documents:trend_index"))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'href="/trends/LAB_WBC/"' in content
    assert "白细胞计数" in content and "5.0" in content and "10^9/L" in content
    assert "2026年8月20日" in content and "2 次可比较记录" in content
    assert "单次指标" not in content
    assert response["Cache-Control"] == "private, no-store, max-age=0"


def test_trend_index_empty_state_explains_requirement_and_next_actions(django_user_model):
    client, _ = _patient(django_user_model, "index-empty")

    response = client.get(reverse("documents:trend_index"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "暂时没有可生成趋势的指标" in content
    assert "同一指标至少需要两个可比较记录" in content
    assert 'href="/records/"' in content
    assert re.search(r'<a[^>]*href="/uploads/new/"[^>]*>上传新资料</a>', content)


def test_trend_index_is_patient_scoped_and_uses_neutral_semantic_markup(django_user_model):
    client, patient = _patient(django_user_model, "index-isolated")
    _other_client, other = _patient(django_user_model, "index-other")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(patient, date(2026, 8, 1), "4.6")
    _observation(other, date(2026, 7, 1), "99.1", standard_name="其他患者指标")
    _observation(other, date(2026, 9, 1), "99.2", standard_name="其他患者指标")

    content = client.get(reverse("documents:trend_index")).content.decode()

    assert "99.1" not in content and "99.2" not in content and "其他患者指标" not in content
    assert content.count("<h1") == 1
    assert '<ol class="trend-summary-list">' in content
    assert '<time datetime="2026-08-01">2026年8月1日</time>' in content
    assert "改善" not in content and "恶化" not in content and "持续升高" not in content


def test_trend_index_excludes_missing_unit_results(django_user_model):
    client, patient = _patient(django_user_model, "index-unit")
    _observation(patient, date(2026, 7, 1), "4.2", raw_unit="")
    _observation(patient, date(2026, 8, 1), "4.6", raw_unit="")

    content = client.get(reverse("documents:trend_index")).content.decode()

    assert "暂时没有可生成趋势的指标" in content


def test_trend_index_requires_authentication(client):
    response = client.get("/trends/")

    assert response.status_code == 302
    assert response["Location"] == "/login/?next=/trends/"


def test_detail_trend_returns_to_trend_index(django_user_model):
    client, patient = _patient(django_user_model, "index-return")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(patient, date(2026, 8, 1), "4.6")

    content = client.get("/trends/LAB_WBC/").content.decode()

    assert 'href="/trends/"' in content
    assert "返回趋势总览" in content
