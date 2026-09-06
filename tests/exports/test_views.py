import json
from datetime import date

import pytest
from django.test import Client

from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.factories import parsed_facts
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db
SELECTION = {"mode": "all", "nickname": "合成昵称", "sections": ["patient", "diagnosis", "treatment", "labs", "imaging", "sources"]}


def test_preview_generate_download_and_cancel_page_flow(django_user_model, monkeypatch):
    client, patient = _patient(django_user_model, "visit-view")
    document, version = parsed_facts(patient, ["诊断：未见明确异常。"])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    _observation(patient, date(2026, 8, 20), "<4.2", result_type="COMPARATOR")
    assert "就诊准备与导出" in client.get("/records/").content.decode()
    client.get("/visit/")
    response = client.post("/visit/", {**SELECTION, "action": "preview", "details": "on"})
    assert response.status_code == 302
    location = response["Location"]
    job = ExportJob.objects.get()
    preview = client.get(location)
    assert preview.status_code == 200
    assert "未见明确异常" in preview.content.decode()
    assert "&lt;4.2" in preview.content.decode()
    assert "静态文件无法撤回" in preview.content.decode()
    assert preview["Cache-Control"] == "private, no-store, max-age=0"
    pdf = client.get(location + "pdf/")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    monkeypatch.setattr("apps.exports.views.safe_enqueue_export", lambda _: None)
    assert client.post(location, {"format": "json"}).status_code == 302
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    monkeypatch.setattr("apps.exports.views.get_object_store", lambda: store)
    download = client.get(location + "download/")
    assert download.status_code == 200
    assert download["Content-Disposition"] == 'attachment; filename="records.json"'
    assert json.loads(download.content)["facts"][0]["id"] == str(fact.pk)
    assert client.get(location + "cancel/").status_code == 405
    assert client.post(location + "cancel/").status_code == 302
    assert client.get(location + "download/").status_code == 409


def test_date_filter_unknown_choice_cross_patient_and_no_empty_success(django_user_model):
    client, patient = _patient(django_user_model, "visit-view-dates")
    unknown, _ = _document(patient)
    client.get("/visit/")
    selection = {**SELECTION, "mode": "dates", "start": "2026-08-01", "end": "2026-08-31"}
    result = client.post("/visit/", {**selection, "action": "filter"})
    assert result.status_code == 200
    assert "已选 0 份" in result.content.decode()
    assert str(unknown.pk) in result.content.decode()
    assert client.post("/visit/", {**selection, "action": "preview"}).status_code == 400
    assert not ExportJob.objects.exists()
    result = client.post("/visit/", {**selection, "unknown_ids": [str(unknown.pk)], "action": "preview"})
    assert result.status_code == 302
    foreign, _ = _patient(django_user_model, "visit-view-foreign")
    assert foreign.get(result["Location"]).status_code == 404
    assert foreign.get(result["Location"] + "pdf/").status_code == 404
    assert foreign.post(result["Location"] + "cancel/").status_code == 404
    assert foreign.get(result["Location"] + "download/").status_code == 404
    assert client.get("/visit/?edit=invalid").status_code == 404


def test_visit_forms_require_csrf_and_render_user_text_as_text(django_user_model):
    client, patient = _patient(django_user_model, "visit-csrf")
    _document(patient)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(patient.account)
    assert strict.post("/visit/", {**SELECTION, "action": "preview"}).status_code == 403
    client.get("/visit/")
    result = client.post("/visit/", {**SELECTION, "nickname": "<script>test()</script>", "action": "preview"})
    page = client.get(result["Location"]).content.decode()
    assert "<script>test()</script>" not in page
    assert "&lt;script&gt;test()&lt;/script&gt;" in page
