"""Actual rendered choices, private downloads and scoped share lifecycle."""
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.patients.models import PatientShare
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import review
from tests.exports.test_pathology_exports import _graph, selection
from tests.patients.test_family_shares import ShareLink, exchange


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("destination", ["export", "share"])
def test_real_selector_discloses_mandatory_score_qualifiers_before_submission(django_user_model, destination):
    client, patient, _, _, fields = _graph(django_user_model, "path-choice-" + destination)
    url = "/visit/" if destination == "export" else f"/patients/{patient.pk}/shares/"
    response = client.get(url)
    assert response.status_code == 200
    label = dict(response.context["form"].fields["clinical_field_ids"].choices)[str(fields["cps"].pk)]
    assert "PD-L1 CPS 21" in label and "选定标本" in label and "选定检测" in label
    assert "单位未印刷" in label and "不可据此判断可比" in label
    assert "标记、评分和标本/检测归属会随所选结果保留" in response.content.decode()


@pytest.mark.parametrize("destination", ["export", "share"])
def test_choice_render_drops_old_body_if_unselected_context_changes_mid_read(django_user_model, monkeypatch, destination):
    client, patient, _, _, fields = _graph(django_user_model, "path-choice-race-" + destination)
    if destination == "export":
        from apps.exports import views
        target, original = "apps.exports.views._render", views._render
        url = "/visit/"
    else:
        from apps.patients import share_views
        target, original = "apps.patients.share_views.render", share_views.render
        url = f"/patients/{patient.pk}/shares/"
    changed = []

    def during(*args, **kwargs):
        response = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CLONE-NEW"}, "raw_value": "SYN-CLONE-NEW"})
        return response

    monkeypatch.setattr(target, during)
    response = client.get(url)
    assert response.status_code == 409
    assert "CPS 21" not in response.content.decode()
    assert response["Cache-Control"] == "private, no-store, max-age=0"


@pytest.mark.django_db(transaction=True)
def test_http_preview_pdf_generate_json_and_download_keep_selected_unit_and_context(django_user_model, monkeypatch):
    import fitz

    client, patient, document, _, fields = _graph(django_user_model, "path-output-http")
    client.get("/visit/")
    response = client.post("/visit/", {**selection(document, fields["cps"]), "nickname": "合成昵称",
                                       "custom_clinical_fields": "on", "action": "preview", "details": "on"})
    assert response.status_code == 302
    url = response["Location"]
    page = client.get(url)
    assert page.status_code == 200 and "PD-L1 CPS 21" in page.content.decode()
    assert "SYN-CLONE-A" not in page.content.decode()
    pdf = client.get(url + "pdf/")
    assert pdf.status_code == 200
    payload = b"".join(pdf.streaming_content)
    pdf.close()
    with fitz.open(stream=payload, filetype="pdf") as pages:
        text = "".join(page.get_text() for page in pages)
    assert "CPS 21" in text and "PD-L1" in text
    assert "选定标本" in text and "选定检测" in text and "单位未印刷" in text
    assert "SYN-CLONE-A" not in text and "标本甲" not in text
    monkeypatch.setattr("apps.exports.views.safe_enqueue_export", lambda _: None)
    assert client.post(url, {"format": "json"}).status_code == 302
    job = ExportJob.objects.get(patient=patient)
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    monkeypatch.setattr("apps.exports.views.get_object_store", lambda: store)
    response = client.get(url + "download/")
    assert response.status_code == 200
    data = json.loads(b"".join(response.streaming_content))
    response.close()
    assert len(data["clinical_fields"]) == 1
    assert data["clinical_fields"][0]["content"]["semantic_qualifiers"]["marker"]["label"] == "PD-L1"
    review(patient, fields["clone"], "REVOKE")
    assert client.get(url).status_code == 409
    assert client.get(url + "download/").status_code == 409


@pytest.mark.parametrize("change", ["clone", "new_member", "parent", "author"])
def test_http_fine_share_keeps_required_meaning_and_stops_after_dependency_change(django_user_model, change):
    from apps.accounts.deletion import purge_account_deletion, request_account_deletion
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import revise_report
    from apps.patients.models import PatientMembership
    from tests.facts.pathology_factories import add_field, confirm_graph

    client, patient, document, report, fields = _graph(django_user_model, "path-share-http-" + change)
    if change == "author":
        _, collaborator = _patient(django_user_model, "path-share-former-author")
        PatientMembership.objects.create(patient=patient, account=collaborator.account, role="EDITOR")
        review(patient, fields["clone"], actor=collaborator.account)
        confirm_graph(patient, fields)
    response = client.post(f"/patients/{patient.pk}/shares/", {
        **selection(document, fields["cps"]), "expires_in_hours": 24,
    })
    assert response.status_code == 201
    parser = ShareLink()
    parser.feed(response.content.decode())
    token = parse_qs(urlsplit(parser.value).fragment)["token"][0]
    viewer, _ = _patient(django_user_model, "path-share-viewer-" + change)
    identity = exchange(viewer, token)
    url = f"/shared/{identity}/"
    page = viewer.get(url)
    assert page.status_code == 200 and "PD-L1 CPS 21" in page.content.decode()
    assert "选定标本" in page.content.decode() and "选定检测" in page.content.decode()
    assert "SYN-CLONE-A" not in page.content.decode() and "标本甲" not in page.content.decode()
    assert viewer.get(url + f"documents/{document.pk}/").status_code == 404
    assert viewer.get(url + f"documents/{document.pk}/original/").status_code == 404
    if change == "clone":
        review(patient, fields["clone"], "REVOKE")
    elif change == "new_member":
        add_field(patient, report, "assay.method", "assay:a", {"code": "IHC", "raw": "IHC"},
                  {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]})
    elif change == "parent":
        revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=0,
                      expected_source=report_source_token(report))
    else:
        job = request_account_deletion(collaborator.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        assert purge_account_deletion(job.pk).outcome == "PURGED"
    response = viewer.get(url)
    assert response.status_code == 410 and "CPS 21" not in response.content.decode()
    assert PatientShare.objects.get(pk=identity).snapshot == {}
