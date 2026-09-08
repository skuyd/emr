import uuid

import pytest

from apps.exports.errors import ExportUnavailable
from apps.exports.services import create_preview, get_preview
from apps.patients.sharing import create_share, exchange_share_token
from apps.treatments.services import revise_event
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_access import family
from tests.treatments.test_manual_events import create
from tests.exports.test_treatment_exports import selection


pytestmark = pytest.mark.django_db


def test_selected_user_event_has_real_export_binding_and_revision_scrubs_snapshot_immediately(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, "treatment-output-revision")
    event = create(patient, actor)
    job = create_preview(patient, client.session.session_key, selection(treatment_event_ids=[str(event.pk)]), actor=actor)
    assert job.treatment_sources.get(event=event).event_id == event.pk
    revise_event(patient, event.pk, actor=actor, action="CORRECT", expected_revision=1,
                 operation_id=uuid.uuid4(), checked_original=True, changes={"note": "更新补记"})
    job.refresh_from_db()
    assert job.snapshot == {} and job.status == "INVALIDATED" and job.cleanup_pending
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=actor)


def test_treatment_only_share_is_limited_to_selected_current_values_and_normal_routes_remain_denied(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, "treatment-share-limited")
    event = create(patient, actor, title="明确分享的本人记录")
    create(patient, actor, title="未选择的私人治疗记录")
    created = create_share(patient, patient.account, selection(treatment_event_ids=[str(event.pk)]))
    reader, own = _patient(django_user_model, "treatment-share-recipient")
    reader.get("/shared/open/")
    exchange_share_token(created.token, own.account, reader.session.session_key)
    response = reader.get(f"/shared/{created.share.pk}/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "明确分享的本人记录" in html and "未选择的私人治疗记录" not in html
    assert "/treatments/events/" not in html and str(actor.pk) not in html
    assert reader.get(f"/treatments/events/{event.pk}/").status_code == 404
    assert created.share.treatment_sources.get(event=event).event_id == event.pk
    revise_event(patient, event.pk, actor=actor, action="REVOKE", expected_revision=1, operation_id=uuid.uuid4())
    created.share.refresh_from_db()
    assert created.share.snapshot == {} and created.share.invalidated_at is not None
    assert reader.get(f"/shared/{created.share.pk}/").status_code == 410


def test_physical_user_event_delete_removes_previously_copied_medical_content(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, "treatment-output-purge")
    event = create(patient, actor)
    job = create_preview(patient, client.session.session_key, selection(treatment_event_ids=[str(event.pk)]), actor=actor)
    event.delete()
    job.refresh_from_db()
    assert job.snapshot == {} and job.status == "INVALIDATED"


def test_fine_treatment_share_cannot_open_originals(django_user_model):
    from apps.exports.errors import ExportInputError
    from tests.documents.test_detail_viewer import _document
    _, patient = _patient(django_user_model, "treatment-share-original-scope")
    event = create(patient, patient.account)
    document, _ = _document(patient)
    with pytest.raises(ExportInputError):
        create_share(patient, patient.account, selection([document], treatment_event_ids=[str(event.pk)], sections=["treatment", "sources"]),
                     allow_original_download=True)
