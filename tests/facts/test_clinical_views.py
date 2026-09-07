import pytest

from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from tests.facts.test_clinical_foundation import clinical_fixture


pytestmark = pytest.mark.django_db


def _text_post(patient, fact, *, action="CORRECT", text="合成更正唯一检索词"):
    row = effective_fact(fact)
    return {"patient_id": str(patient.pk), "action": action, "expected_revision": fact.revision_number,
            "expected_source": row["current_source_token"], "raw_value": text, "text_value": text,
            "checked_original": "on"}


def test_field_pages_correct_search_select_and_invalidate_export(django_user_model):
    from apps.exports.forms import SelectionForm
    from apps.exports.services import create_preview

    client, patient, document, version, _ = clinical_fixture(django_user_model, name="clinical-pages")
    fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    report_url = f"/facts/reports/{fact.clinical_report_id}/"
    field_url = f"/facts/{fact.pk}/"
    page = client.get(field_url)
    assert page.status_code == 200
    assert "尚未核对" in page.content.decode()
    assert "双肺结节，建议结合临床。" in page.content.decode()
    assert client.get(report_url).status_code == 200
    response = client.post(field_url, _text_post(patient, fact))
    assert response.status_code == 302
    fact.refresh_from_db()
    assert fact.revisions.get().author_id == patient.account_id
    assert effective_fact(fact)["usable"]
    detail = client.get(f"/records/{document.pk}/")
    assert "合成更正唯一检索词" in detail.content.decode()
    results = client.get("/records/", {"q": "合成更正唯一检索词"})
    assert results.status_code == 200
    assert results.context["page_obj"].paginator.count == 1
    assert "已核对" in results.content.decode()
    form = SelectionForm(patient, {"mode": "documents", "document_ids": [str(document.pk)],
                                  "nickname": "合成", "custom_clinical_fields": "on", "clinical_field_ids": [str(fact.pk)]})
    assert form.is_valid(), form.errors
    assert form.selection()["clinical_field_ids"] == [str(fact.pk)]
    preview = create_preview(patient, client.session.session_key, form.selection(), actor=patient.account)
    assert preview.snapshot["clinical_fields"][0]["id"] == str(fact.pk)
    assert client.get("/visit/").status_code == 200
    assert client.post(field_url, _text_post(patient, fact, text="新修订唯一词")).status_code == 302
    assert client.get("/records/", {"q": "合成更正唯一检索词"}).context["page_obj"].paginator.count == 0
    preview.refresh_from_db()
    assert preview.status == "INVALIDATED"


def test_structured_routes_resolve_patient_and_recheck_reader_revocation(django_user_model):
    from apps.patients.models import PatientMembership
    from apps.patients.access import change_membership
    from tests.documents.test_detail_viewer import _patient

    _, patient, document, version, _ = clinical_fixture(django_user_model, name="clinical-owner")
    client, other = _patient(django_user_model, "clinical-reader")
    membership = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    routes = [f"/facts/{fact.pk}/", f"/facts/reports/{fact.clinical_report_id}/", f"/facts/documents/{document.pk}/reports/"]
    for url in routes:
        response = client.get(url)
        assert response.status_code == 200
        assert 'name="action"' not in response.content.decode()
        assert client.get(url, {"patient": str(other.pk)}).status_code == 404
        assert client.post(url, _text_post(patient, fact)).status_code == 403
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    for url in routes:
        assert client.get(url).status_code == 404
    assert fact.revisions.count() == 0


def test_access_revoked_while_rendering_cannot_return_clinical_body(django_user_model, monkeypatch):
    from apps.facts import views
    from apps.patients.models import PatientMembership
    from apps.patients.access import change_membership
    from tests.documents.test_detail_viewer import _patient

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="clinical-inflight-owner")
    client, other = _patient(django_user_model, "clinical-inflight-reader")
    membership = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    original = views.render

    def revoke_during_render(*args, **kwargs):
        response = original(*args, **kwargs)
        change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return response

    monkeypatch.setattr(views, "render", revoke_during_render)
    assert client.get(f"/facts/reports/{document.clinical_reports.get().pk}/").status_code == 403
