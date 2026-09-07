import json
import uuid

import pytest
from django.urls import reverse

from apps.operations.models import AuditEvent
from apps.patients.models import PatientMembership
from apps.treatments.models import TreatmentDerivationRun, TreatmentEvent
from tests.documents.test_detail_viewer import _document, _patient
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create
from tests.treatments.test_regimens import regimen

pytestmark = pytest.mark.django_db


def event_data(patient, **extra):
    return {"patient_id": str(patient.pk), "operation_id": str(uuid.uuid4()), "title": "本人治疗记录",
            "kind": "SYSTEMIC_TREATMENT", "occurrence": "OCCURRED", "date": "2024-02-29", "date_precision": "DAY",
            "regimen_text": "本人记录方案", "cycle_ordinal": "", "cycle_day": "", "note": "", **extra}


def test_workspace_is_reachable_from_records_and_get_never_persists_proposals(django_user_model):
    client, patient = _patient(django_user_model, "treatment-workspace")
    create(patient, patient.account)
    before = TreatmentEvent.objects.count()
    response = client.get("/treatments/")
    assert response.status_code == 200
    assert "自动周期提议" in response.content.decode()
    assert "no-store" in response.headers["Cache-Control"]
    assert TreatmentEvent.objects.count() == before and TreatmentDerivationRun.objects.count() == 0
    assert '/treatments/' in client.get(reverse("documents:records")).content.decode()


@pytest.mark.parametrize("name", ["event_new", "regimen_new", "cycle_new", "merge"])
def test_creation_and_organization_forms_are_real_patient_pages(django_user_model, name):
    client, patient = _patient(django_user_model, "treatment-form-" + name)
    response = client.get(reverse("treatments:" + name))
    assert response.status_code == 200
    assert f'value="{patient.pk}"' in response.content.decode()
    assert 'name="operation_id"' in response.content.decode()


def test_create_patient_event_returns_actual_record_and_invalid_date_stays_form_error(django_user_model):
    client, patient = _patient(django_user_model, "treatment-create-http")
    response = client.post("/treatments/events/new/", event_data(patient, date="2024-02-30"))
    assert response.status_code == 400 and TreatmentEvent.objects.count() == 0
    response = client.post("/treatments/events/new/", event_data(patient))
    assert response.status_code == 302
    event = TreatmentEvent.objects.get()
    assert event.created_by_id == patient.account_id and event.current_content["date"] == "2024-02-29"
    detail = client.get(response["Location"])
    assert detail.status_code == 200 and "本人治疗记录" in detail.content.decode()


def test_member_roles_and_real_resource_patient_bind_all_old_tab_routes(django_user_model):
    owner, patient = _patient(django_user_model, "treatment-role-owner")
    member, other = _patient(django_user_model, "treatment-role-viewer")
    PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    event = create(patient, patient.account)
    scheme = regimen(patient, event)
    item = cycle(patient, [event], regimen_id=scheme.pk)
    for name, identity in [("event", event.pk), ("regimen", scheme.pk), ("cycle", item.pk), ("split", item.pk), ("assign", item.pk)]:
        path = reverse("treatments:" + name, args=[identity])
        assert member.get(path).status_code == 200
        assert member.get(path, {"patient": str(other.pk)}).status_code == 404
        assert member.post(path, {"patient_id": str(patient.pk), "action": "REJECT"}).status_code == 403
    assert member.post("/treatments/events/new/", event_data(patient)).status_code == 403


def test_foreign_cycle_read_denied_audit_uses_target_patient(django_user_model):
    from apps.operations.audit import _hash
    _, patient = _patient(django_user_model, "treatment-audit-target")
    intruder, other = _patient(django_user_model, "treatment-audit-other")
    item = cycle(patient, [create(patient, patient.account)])
    response = intruder.get(reverse("treatments:cycle", args=[item.pk]))
    assert response.status_code == 404
    event = AuditEvent.objects.filter(action="treatment_cycle_viewed", result="denied").latest("created_at")
    assert event.patient_hash == _hash("patient", patient.pk) and event.resource_type == "treatment_cycle"


def test_cycle_form_stale_revision_conflicts_without_overwriting(django_user_model):
    client, patient = _patient(django_user_model, "treatment-stale-http")
    item = cycle(patient, [create(patient, patient.account)])
    path = reverse("treatments:cycle", args=[item.pk])
    response = client.post(path, {"patient_id": str(patient.pk), "operation_id": str(uuid.uuid4()), "expected_revision": 0,
        "action": "REVOKE", "anchor": "2024-02-29", "anchor_precision": "DAY", "end": "", "end_precision": "UNKNOWN",
        "ordinal": "", "regimen_id": "", "expected_sources": "{}", "note": ""})
    assert response.status_code == 409
    item.refresh_from_db()
    assert item.current_content["status"] == "CONFIRMED" and item.revision_number == 1


def test_single_proposal_confirmation_is_source_bound_and_has_history(django_user_model):
    from apps.treatments.derivations import proposal_preview
    client, patient = _patient(django_user_model, "treatment-confirm-http")
    create(patient, patient.account)
    preview = proposal_preview(patient, actor=patient.account)
    proposed = preview["proposals"]["cycles"][0]
    response = client.post("/treatments/", {"patient_id": str(patient.pk), "action": "CONFIRM_PROPOSAL", "checked_original": "on",
        "operation_id": str(uuid.uuid4()), "expected_fingerprint": preview["input_fingerprint"],
        "proposal_id": proposed["id"], "expected_revision": 0})
    assert response.status_code == 302
    assert TreatmentDerivationRun.objects.count() == 1
    response = client.get(response["Location"])
    assert response.status_code == 200 and "自动提议原稿" in response.content.decode() and "修订记录" in response.content.decode()


def test_html_escapes_patient_entered_text_and_reads_audit_once(django_user_model):
    client, patient = _patient(django_user_model, "treatment-escape-http")
    event = create(patient, patient.account, title='<script>alert("x")</script>')
    before = AuditEvent.objects.filter(action="treatment_event_viewed").count()
    response = client.get(reverse("treatments:event", args=[event.pk]))
    body = response.content.decode()
    assert '<script>alert("x")</script>' not in body and '&lt;script&gt;' in body
    assert AuditEvent.objects.filter(action="treatment_event_viewed").count() == before + 1


def test_event_page_links_to_actual_immutable_original_source(django_user_model):
    from tests.treatments.factories import source_event
    from apps.treatments.readmodels import effective_event
    client, patient = _patient(django_user_model, "treatment-source-http")
    event, fact, document = source_event(patient)
    source = effective_event(event)["sources"][0]
    response = client.get(reverse("treatments:event", args=[event.pk]))
    body = response.content.decode()
    from django.utils.html import escape
    assert f'href="{escape(source["url"])}"' in body
    assert fact.raw_text in body


@pytest.mark.parametrize("detail", [False, True])
def test_rendering_read_rechecks_the_same_patient_after_membership_revocation(django_user_model, monkeypatch, detail):
    from apps.patients.access import change_membership
    from apps.treatments import views
    _, patient = _patient(django_user_model, "treatment-render-owner")
    client, other = _patient(django_user_model, "treatment-render-member")
    member = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    event = create(patient, patient.account)
    original = views.render
    def revoke_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=member.revision)
        return response
    monkeypatch.setattr(views, "render", revoke_after_render)
    path = reverse("treatments:event", args=[event.pk]) if detail else reverse("treatments:index")
    response = client.get(path, {"patient": str(patient.pk)})
    assert response.status_code == 403
    assert event.current_content["title"] not in response.content.decode()
