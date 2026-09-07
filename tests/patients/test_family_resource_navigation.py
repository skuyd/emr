import html
import re
from datetime import date

import pytest

from apps.patients.models import Patient
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def two_patients(django_user_model, identity):
    client, first = _patient(django_user_model, identity)
    second = Patient.objects.create(account=first.account, display_name="Another synthetic patient")
    assert client.post(f"/patients/{second.pk}/select/").status_code == 302
    return client, first, second


@pytest.mark.parametrize("selected", [True, False])
def test_push_notification_opens_its_patient_after_session_switch(django_user_model, selected):
    from apps.notifications.models import TaskNotification

    client, first, second = two_patients(django_user_model, "family-push-link-" + str(selected))
    if not selected:
        session = client.session
        session.pop("active_patient_id", None)
        session.save()
    document, _ = _document(first)
    note = TaskNotification.objects.create(patient=first, batch=document.batch, kind="COMPLETED")
    # The service worker carries only the opaque notification id, no patient data.
    response = client.get(f"/notifications/{note.pk}/open/")
    assert response.status_code == 302
    target = client.get(response.url)
    assert target.context["request"].patient.pk == first.pk
    assert client.session.get("active_patient_id") == (str(second.pk) if selected else None)


@pytest.mark.parametrize("embed", [False, True])
def test_old_patient_viewer_image_and_thumbnail_requests_survive_session_switch(django_user_model, monkeypatch, embed):
    client, first, _ = two_patients(django_user_model, "family-viewer-link-" + str(embed))
    document, _ = _document(first)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic original"
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.render_page", lambda *args, **kwargs: b"synthetic raster")
    monkeypatch.setattr("apps.documents.views.originals.render_thumbnail_sheet", lambda *args, **kwargs: b"synthetic sheet")
    response = client.get(f"/records/{document.pk}/viewer/?patient={first.pk}" + ("&embed=1" if embed else ""))
    assert response.status_code == 200
    source = html.unescape(re.search(r'<img src="([^"]+)"[^>]*data-viewer-image', response.content.decode()).group(1))
    assert client.get(source).status_code == 200
    assert client.get(response.context["thumbnail_sheet_url"]).status_code == 200


def test_resource_navigation_retains_facts_labs_batch_and_export_context(django_user_model):
    from apps.exports.services import create_preview
    from apps.facts.models import Fact
    from tests.facts.factories import parsed_facts
    from tests.labs.test_trends import _observation

    client, first, _ = two_patients(django_user_model, "family-resource-links")
    document, version = parsed_facts(first, ["诊断：合成诊断。"])
    fact = Fact.objects.get(parsing_version=version)
    _, observation = _observation(first, date(2026, 8, 1), "4")
    job = create_preview(first, client.session.session_key, {"mode": "all"}, actor=first.account)
    for path in (
        f"/facts/documents/{document.pk}/", f"/facts/{fact.pk}/",
        f"/labs/observations/{observation.pk}/", f"/labs/observations/{observation.pk}/source/raw_value/",
        f"/api/upload-batches/{document.batch_id}/status/", f"/visit/{job.pk}/",
    ):
        assert client.get(path).status_code == 200, path
    preview = client.get(f"/visit/{job.pk}/")
    edit_links = re.findall(r'href="([^\"]*[?&]edit=[^\"]+)"', html.unescape(preview.content.decode()))
    assert edit_links
    for link in edit_links:
        target = client.get(link)
        assert target.status_code == 200
        assert target.context["request"].patient.pk == first.pk
        assert target.context["form"].initial["mode"] == "all"


@pytest.mark.parametrize("channel", ["query", "header"])
def test_resource_identity_cannot_override_an_explicit_different_patient(django_user_model, channel):
    client, first, second = two_patients(django_user_model, "family-explicit-resource-" + channel)
    document, _ = _document(first)
    path = f"/records/{document.pk}/viewer/"
    response = client.get(path, {"patient": second.pk}) if channel == "query" else client.get(path, HTTP_X_PATIENT_ID=str(second.pk))
    assert response.status_code == 404
    assert client.post(f"/records/{document.pk}/delete/", {"confirmation": "delete"}).status_code == 409
    document.refresh_from_db()
    assert document.deleted_at is None


def test_non_resource_navigation_retains_the_patient_in_the_old_page(django_user_model):
    client, first, _ = two_patients(django_user_model, "family-navigation-links")
    response = client.get(f"/me/?patient={first.pk}")
    source = html.unescape(re.search(r'href="(/records/[^\"]*)"', response.content.decode()).group(1))
    target = client.get(source)
    assert target.context["request"].patient.pk == first.pk


def test_manual_fact_without_parsing_version_uses_its_document_patient(django_user_model):
    from apps.facts.revisions import add_manual_fact

    client, first, _ = two_patients(django_user_model, "family-manual-fact-link")
    document, _ = _document(first)
    fact = add_manual_fact(first, document.pk, page_number=1, category="DIAGNOSIS", text="Synthetic source excerpt", actor=first.account)
    assert fact.parsing_version_id is None
    assert client.get(f"/facts/{fact.pk}/").status_code == 200


@pytest.mark.parametrize("path", ["/records/", "/labs/compare/"])
def test_get_filters_retain_the_patient_when_browser_replaces_url_query(django_user_model, path):
    client, first, _ = two_patients(django_user_model, "family-filter-" + path)
    response = client.get(path, {"patient": str(first.pk)})
    form = re.search(r'<form[^>]*method="get"[^>]*>(.*?)</form>', response.content.decode(), re.DOTALL).group(1)
    patient_input = re.search(r'<input[^>]*name="patient"[^>]*value="([^\"]+)"', form)
    assert patient_input is not None
    target = client.get(path, {"patient": patient_input.group(1)})
    assert target.context["request"].patient.pk == first.pk
