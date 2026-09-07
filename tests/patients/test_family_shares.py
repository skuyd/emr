"""A shared source remains a limited grant through rendering and downloading."""

from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.utils import timezone

from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient, _png_bytes
from tests.documents.fakes import InMemoryObjectStore


pytestmark = pytest.mark.django_db


class ShareLink(HTMLParser):
    value = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "share_link":
            self.value = attrs.get("value", "")


def create_link(client, patient, document, *, sections=None, originals=False, hours=24):
    response = client.post(f"/patients/{patient.pk}/shares/", {
        "document_ids": [str(document.pk)], "sections": sections or ["patient", "diagnosis", "sources"],
        "expires_in_hours": hours, "allow_original_download": "on" if originals else "",
    })
    assert response.status_code == 201
    parser = ShareLink()
    parser.feed(response.content.decode())
    link = urlsplit(parser.value)
    assert link.path == "/shared/open/" and not link.query
    return parse_qs(link.fragment)["token"][0]


def exchange(client, token):
    # Mirror the browser landing request, which persists the initialized login
    # lifetime before the separate token exchange request.
    assert client.get("/shared/open/").status_code == 200
    response = client.post("/shared/exchange/", {"token": token})
    assert response.status_code == 200
    return response.json()["share_id"]


def test_share_limits_selected_documents_and_does_not_grant_family_membership(django_user_model):
    owner, patient = _patient(django_user_model, "share-scope-owner")
    viewer, own = _patient(django_user_model, "share-scope-reader")
    chosen, _ = _document(patient)
    other, _ = _document(patient, content_type="image/png")
    token = create_link(owner, patient, chosen)
    share_id = exchange(viewer, token)
    page = viewer.get(f"/shared/{share_id}/")
    assert page.status_code == 200 and chosen.display_filename in page.content.decode()
    assert other.display_filename not in page.content.decode() and str(other.pk) not in page.content.decode()
    assert not PatientMembership.objects.filter(patient=patient, account=own.account).exists()
    assert viewer.get(f"/records/{chosen.pk}/").status_code == 404
    assert viewer.get(f"/records/{chosen.pk}/original/").status_code == 404
    assert viewer.get(f"/shared/{share_id}/documents/{other.pk}/").status_code == 404
    assert viewer.get(f"/shared/{share_id}/documents/{chosen.pk}/original/").status_code == 403


def test_share_duration_login_and_creating_role_are_enforced(django_user_model):
    from apps.patients.models import PatientShare

    owner, patient = _patient(django_user_model, "share-policy-owner")
    writer, own = _patient(django_user_model, "share-policy-editor")
    PatientMembership.objects.create(patient=patient, account=own.account, role="EDITOR")
    document, _ = _document(patient)
    response = writer.post(f"/patients/{patient.pk}/shares/", {"document_ids": [str(document.pk)], "sections": ["sources"]})
    assert response.status_code == 403
    token = create_link(owner, patient, document)
    share = PatientShare.objects.get(patient=patient)
    assert (share.expires_at - share.created_at).total_seconds() == 24 * 3600
    assert token not in repr(PatientShare.objects.values().get(pk=share.pk))
    assert Client().post("/shared/exchange/", {"token": token}).status_code == 401
    too_long = owner.post(f"/patients/{patient.pk}/shares/", {
        "document_ids": [str(document.pk)], "sections": ["sources"], "expires_in_hours": 169,
    })
    assert too_long.status_code == 400 and PatientShare.objects.filter(patient=patient).count() == 1


@pytest.mark.parametrize("reason", ["revoke", "expire", "trash", "creator_role", "new_manual_fact", "patient_delete"])
def test_share_rechecks_lifecycle_permissions_and_manual_source_fingerprint(django_user_model, reason):
    from apps.patients.models import PatientShare
    from apps.patients.deletion import request_patient_deletion
    from apps.documents.lifecycle import move_to_trash
    from apps.facts.revisions import add_manual_fact

    owner, patient = _patient(django_user_model, "share-live-owner")
    administrator, admin_own = _patient(django_user_model, "share-live-admin")
    member = PatientMembership.objects.create(patient=patient, account=admin_own.account, role="ADMIN")
    viewer, _ = _patient(django_user_model, "share-live-reader")
    document, _ = _document(patient)
    token = create_link(administrator, patient, document)
    share_id = exchange(viewer, token)
    if reason == "revoke":
        assert owner.post(f"/patients/{patient.pk}/shares/{share_id}/revoke/").status_code == 302
    elif reason == "expire":
        PatientShare.objects.filter(pk=share_id).update(expires_at=timezone.now() - timedelta(seconds=1))
    elif reason == "trash":
        move_to_trash(patient, document.pk, actor=patient.account)
    elif reason == "creator_role":
        change_membership(patient, patient.account, member.pk, role="EDITOR", expected_revision=0)
    elif reason == "patient_delete":
        request_patient_deletion(patient.pk, patient.account, document_dispatch=lambda _: None)
    else:
        fact = add_manual_fact(patient, document.pk, page_number=1, category="DIAGNOSIS", text="合成的人工事实。", actor=patient.account)
        assert fact.parsing_version_id is None
    assert viewer.get(f"/shared/{share_id}/").status_code == 410
    share = PatientShare.objects.get(pk=share_id)
    assert share.snapshot == {} and (share.invalidated_at or share.revoked_at)
    assert viewer.post("/shared/exchange/", {"token": token}).status_code == 410


def test_share_grant_is_bound_to_its_login_session(django_user_model):
    owner, patient = _patient(django_user_model, "share-session-owner")
    viewer, own = _patient(django_user_model, "share-session-reader")
    document, _ = _document(patient)
    share_id = exchange(viewer, create_link(owner, patient, document))
    another_session = Client()
    another_session.force_login(own.account)
    assert another_session.get(f"/shared/{share_id}/").status_code == 404
    viewer.post("/logout/")
    assert viewer.get(f"/shared/{share_id}/").status_code in {302, 401}


def test_shared_original_stops_after_revocation_without_sending_more_bytes(django_user_model, monkeypatch):
    from apps.patients.sharing import revoke_share

    owner, patient = _patient(django_user_model, "share-stream-owner")
    viewer, _ = _patient(django_user_model, "share-stream-reader")
    document, _ = _document(patient)
    share_id = exchange(viewer, create_link(owner, patient, document, originals=True))
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"x" * (1024 * 1024)
    monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
    response = viewer.get(f"/shared/{share_id}/documents/{document.pk}/original/")
    assert response.status_code == 200 and response.file_to_stream is None
    chunks = iter(response.streaming_content)
    assert len(next(chunks)) == 256 * 1024
    revoke_share(patient, patient.account, share_id)
    assert b"".join(chunks) == b""


def test_shared_page_image_checks_permission_after_rendering(django_user_model, monkeypatch):
    from apps.patients.sharing import revoke_share

    owner, patient = _patient(django_user_model, "share-render-owner")
    viewer, _ = _patient(django_user_model, "share-render-reader")
    document, _ = _document(patient, content_type="image/png", page_count=1)
    share_id = exchange(viewer, create_link(owner, patient, document))
    store = InMemoryObjectStore()
    payload = _png_bytes()
    store.objects[document.original_object_key] = payload
    def render_then_revoke(*args, **kwargs):
        revoke_share(patient, patient.account, share_id)
        return payload
    monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
    monkeypatch.setattr("apps.patients.share_views.render_page", render_then_revoke)
    response = viewer.get(f"/shared/{share_id}/documents/{document.pk}/pages/1/image/")
    assert response.status_code == 410 and payload not in response.content


def test_shared_image_and_thumbnail_render_real_private_source_bytes(django_user_model, monkeypatch):
    owner, patient = _patient(django_user_model, "share-real-image-owner")
    viewer, _ = _patient(django_user_model, "share-real-image-reader")
    document, _ = _document(patient, content_type="image/png", page_count=1)
    share_id = exchange(viewer, create_link(owner, patient, document))
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = _png_bytes()
    monkeypatch.setattr("apps.patients.share_views.get_object_store", lambda: store)
    for suffix in ("pages/1/image/", "thumbnails/"):
        response = viewer.get(f"/shared/{share_id}/documents/{document.pk}/{suffix}")
        assert response.status_code == 200 and response.content.startswith(b"\x89PNG")


def test_share_exchange_requires_csrf_and_cleanup_scrubs_expired_snapshot_without_view(django_user_model):
    from apps.patients.models import PatientShare
    from apps.patients.tasks import expire_shared_content

    owner, patient = _patient(django_user_model, "share-expiry-owner")
    _, viewer_own = _patient(django_user_model, "share-expiry-reader")
    document, _ = _document(patient)
    token = create_link(owner, patient, document)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(viewer_own.account)
    assert strict.post("/shared/exchange/", {"token": token}).status_code == 403
    share = PatientShare.objects.get(patient=patient)
    PatientShare.objects.filter(pk=share.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert expire_shared_content() == {"count": 1}
    share.refresh_from_db()
    assert share.snapshot == {} and share.invalidation_reason == "expired"
    assert expire_shared_content() == {"count": 0}
