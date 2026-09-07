"""New material review entries retain the actual family actor in patient audit."""

import pytest

from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from tests.documents.test_material_family import keep_data, material_family


@pytest.mark.django_db
@pytest.mark.parametrize("role,status,result", [("EDITOR", 302, "succeeded"), ("VIEWER", 403, "denied")])
def test_material_review_uses_patient_scoped_actual_actor_audit(django_user_model, monkeypatch, role, status, result):
    _, document, version, client, actor, _ = material_family(django_user_model, f"material-audit-{role}", role)
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", lambda _: None)
    response = client.post(f"/records/{document.pk}/material/", keep_data(document, version))
    assert response.status_code == status
    event = AuditEvent.objects.get(action="document_material_reviewed", result=result,
                                   actor_hash=_hash("actor", actor.pk))
    assert event.patient_hash == _hash("patient", document.patient_id)
    assert event.resource_type == "document" and event.target_hash == _hash("target", document.pk)
    assert event.route_name == "documents:document_material"


@pytest.mark.django_db
def test_material_reset_invalidates_the_previous_share_revision(django_user_model):
    from apps.patients.models import PatientShare
    from apps.patients.sharing import create_share
    from apps.processing.material_review import review_material
    from tests.documents.test_material_views import material_document
    from tests.documents.test_detail_viewer import _patient
    from tests.patients.test_family_shares import exchange

    _, document, version = material_document(django_user_model, "material-share-reset")
    patient = document.patient
    review_material(patient, document.pk, actor=patient.account, action="KEEP_DOCUMENT",
                    expected_version=str(version.pk), expected_revision=0, dispatch=lambda _: None)
    reader, _ = _patient(django_user_model, "material-share-recipient")
    link = create_share(patient, patient.account, {"document_ids": [str(document.pk)], "sections": ["sources"]})
    share_id = exchange(reader, link.token)
    assert reader.get(f"/shared/{share_id}/").status_code == 200
    review_material(patient, document.pk, actor=patient.account, action="AUTO",
                    expected_version=str(version.pk), expected_revision=1, dispatch=lambda _: None)
    assert reader.get(f"/shared/{share_id}/").status_code == 410
    share = PatientShare.objects.get(pk=share_id)
    assert share.invalidated_at and share.snapshot == {}
