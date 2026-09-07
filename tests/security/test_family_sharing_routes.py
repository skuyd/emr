import pytest

from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient
from tests.patients.test_family_shares import create_link, exchange

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "EDITOR", "VIEWER", "OUTSIDER"])
@pytest.mark.parametrize("page", ["members", "invitations", "shares", "audit"])
def test_family_management_pages_enforce_role_matrix(django_user_model, role, page):
    owner, patient = _patient(django_user_model, f"route-owner-{role}-{page}")
    client, own = _patient(django_user_model, f"route-reader-{role}-{page}")
    if role == "OWNER":
        client = owner
    elif role != "OUTSIDER":
        PatientMembership.objects.create(patient=patient, account=own.account, role=role)
    response = client.get(f"/patients/{patient.pk}/{page}/")
    assert response.status_code == (200 if role in {"OWNER", "ADMIN"} else 403)


def test_share_only_reader_cannot_reuse_ordinary_patient_or_management_routes(django_user_model):
    owner, patient = _patient(django_user_model, "route-share-owner")
    reader, _ = _patient(django_user_model, "route-share-reader")
    document, _ = _document(patient)
    share_id = exchange(reader, create_link(owner, patient, document))
    assert reader.get(f"/shared/{share_id}/").status_code == 200
    routes = [f"/records/{document.pk}/", f"/records/{document.pk}/viewer/", f"/records/{document.pk}/original/",
              f"/facts/documents/{document.pk}/", f"/visit/?patient={patient.pk}", f"/labs/compare/?patient={patient.pk}",
              f"/patients/{patient.pk}/members/", f"/patients/{patient.pk}/invitations/",
              f"/patients/{patient.pk}/shares/", f"/patients/{patient.pk}/audit/"]
    for path in routes:
        assert reader.get(path).status_code in {403, 404}, path
    for suffix in ("select", "delete", "members", "invitations", "shares"):
        assert reader.post(f"/patients/{patient.pk}/{suffix}/", {"confirmation": "delete-patient"}).status_code in {403, 404}
