"""Material recovery uses live family access and the actual requesting member."""
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.documents.models import Document, ProcessingRun
from apps.patients.access import change_membership
from apps.patients.models import Patient, PatientMembership
from apps.processing.material_review import review_material
from apps.processing.models import MaterialDecision, ParsingVersion
from apps.processing.runner import run_processing
from tests.documents.test_detail_viewer import _patient
from tests.documents.test_material_views import material_document


pytestmark = pytest.mark.django_db(transaction=True)


def material_family(django_user_model, marker, role="EDITOR"):
    owner, document, version = material_document(django_user_model, marker + "-owner")
    client, own = _patient(django_user_model, marker + "-member")
    membership = PatientMembership.objects.create(patient=document.patient, account=own.account, role=role)
    return owner, document, version, client, own.account, membership


def keep_data(document, version):
    return {"action": "KEEP_DOCUMENT", "expected_version": str(version.pk),
            "expected_revision": "0", "patient_id": str(document.patient_id)}


def keep(document, version, actor, dispatch=lambda _: None):
    return review_material(document.patient, document.pk, actor=actor, action="KEEP_DOCUMENT",
                           expected_version=str(version.pk), expected_revision=0, dispatch=dispatch)


def test_editor_http_recovery_records_actual_actor_and_background_revision(django_user_model, monkeypatch):
    _, document, version, client, actor, membership = material_family(django_user_model, "material-editor")
    queued = []
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", queued.append)
    response = client.post(f"/records/{document.pk}/material/", keep_data(document, version))
    assert response.status_code == 302
    decision = MaterialDecision.objects.get(document=document)
    assert decision.author_id == actor.pk and decision.author_id != document.patient.account_id
    assert decision.processing_run.requested_by_id == actor.pk
    assert decision.processing_run.access_revision == membership.revision
    assert decision.parsing_version_id == version.pk and queued == [decision.processing_run_id]
    assert client.get(response.url).context["request"].patient.pk == document.patient_id


@pytest.mark.parametrize("retained", [False, True])
def test_viewer_reads_suggestion_without_keep_or_reset_controls_and_cannot_post(django_user_model, monkeypatch, retained):
    owner, document, version, client, _, _ = material_family(django_user_model, "material-viewer", "VIEWER")
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", lambda _: None)
    if retained:
        assert owner.post(f"/records/{document.pk}/material/", keep_data(document, version)).status_code == 302
    response = client.get(f"/records/{document.pk}/")
    assert response.status_code == 200 and response.context["material"]["assessed"]
    assert f'action="/records/{document.pk}/material/"' not in response.content.decode()
    before = MaterialDecision.objects.filter(document=document).count()
    for action in ("KEEP_DOCUMENT", "AUTO"):
        assert client.post(f"/records/{document.pk}/material/", {
            **keep_data(document, version), "action": action,
        }).status_code == 403
    assert MaterialDecision.objects.filter(document=document).count() == before


def test_material_old_tab_preserves_explicit_patient_and_rejects_missing_or_wrong_scope(django_user_model, monkeypatch):
    client, document, version = material_document(django_user_model, "material-old-tab")
    assert client.post("/patients/new/", {"display_name": "Second patient", "upload_authority": "on"}).status_code == 302
    second = Patient.objects.get(account=document.patient.account, display_name="Second patient")
    monkeypatch.setattr("apps.documents.views.records.safe_enqueue_processing", lambda _: None)
    url = f"/records/{document.pk}/material/"
    data = keep_data(document, version)
    assert client.post(url, {key: value for key, value in data.items() if key != "patient_id"}).status_code == 409
    assert client.post(url, {**data, "patient_id": str(second.pk)}).status_code == 404
    response = client.post(url, data)
    assert response.status_code == 302
    assert parse_qs(urlparse(response.url).query)["patient"] == [str(document.patient_id)]
    assert client.get(response.url).context["request"].patient.pk == document.patient_id
    second.refresh_from_db()
    assert not Document.objects.filter(patient=second).exists()


@pytest.mark.parametrize("loss", ["revoke", "downgrade", "inactive-account", "deleted-patient"])
def test_cached_member_cannot_recover_after_live_access_loss(django_user_model, loss):
    _, document, version, _, actor, membership = material_family(django_user_model, "material-access-loss")
    if loss == "revoke":
        change_membership(document.patient, document.patient.account, membership.pk, revoke=True, expected_revision=0)
    elif loss == "downgrade":
        change_membership(document.patient, document.patient.account, membership.pk, role="VIEWER", expected_revision=0)
    elif loss == "inactive-account":
        type(actor).objects.filter(pk=actor.pk).update(is_active=False)
    else:
        Patient.objects.filter(pk=document.patient_id).update(deleted_at=timezone.now())
    with pytest.raises(PermissionDenied):
        keep(document, version, actor)
    assert not MaterialDecision.objects.filter(document=document).exists()
    document.refresh_from_db()
    assert document.material_override == "AUTO" and document.material_revision == 0
    assert ProcessingRun.objects.filter(document=document).count() == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_revocation_fences_material_retry_without_publishing_another_version(django_user_model, revoke):
    _, document, version, _, actor, membership = material_family(django_user_model, "material-retry-revocation")
    decision = keep(document, version, actor)
    change_membership(document.patient, document.patient.account, membership.pk,
                      revoke=revoke, role=None if revoke else "VIEWER", expected_revision=0)
    called = []
    run_processing(decision.processing_run_id, lambda _: called.append(True))
    assert called == []
    run = ProcessingRun.objects.get(pk=decision.processing_run_id)
    assert run.error_code == "access_revoked" and not run.is_current
    assert ParsingVersion.objects.filter(document=document).count() == 1
    version.refresh_from_db()
    assert version.active
    document.refresh_from_db()
    assert document.material_override == "KEEP_DOCUMENT"
    assert MaterialDecision.objects.get(document=document).author_id == actor.pk
