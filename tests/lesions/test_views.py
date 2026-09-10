from apps.facts.laterality import review_parent_arguments
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.lesions.models import Lesion, LesionMatchProposal, LesionOperation
from apps.lesions.readmodels import review_observations
from apps.lesions.services import generate_proposals
from apps.patients.models import Patient
from tests.patients.test_family_access import family
from .factories import imaging_observation


pytestmark = pytest.mark.django_db


def family_pair(django_user_model, marker, role="EDITOR"):
    owner, patient, client, actor, member = family(django_user_model, marker, role)
    imaging_observation(django_user_model, patient=patient)
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    return owner, patient, client, actor, member


def submitted_form(response, patient, **changes):
    form = response.context["form"]
    values = {key: form[key].value() if form[key].value() is not None else "" for key in form.fields}
    return {**values, "patient_id": str(patient.pk), **changes}


def test_index_is_read_only_and_explicit_proposal_review_matches_after_patient_switch(django_user_model):
    _, patient, client, actor, _ = family_pair(django_user_model, "lesion-http-match")
    response = client.get("/lesions/", {"patient": str(patient.pk)})
    assert response.status_code == 200
    assert len(response.context["observations"]) == 2 and not LesionMatchProposal.objects.exists()
    generated = client.post("/lesions/propose/", {"patient_id": str(patient.pk)})
    assert generated.status_code == 302 and LesionMatchProposal.objects.count() == 1
    proposal = LesionMatchProposal.objects.get()
    url = f"/lesions/proposals/{proposal.pk}/"
    opened = client.get(url)
    assert opened.status_code == 200
    body = opened.content.decode()
    assert "12mm" in body and "15mm" in body and body.count('title="观察来源原件"') == 2
    data = submitted_form(opened, patient, action="CONFIRM", name="观察 <script>A</script>", checked_original="on")
    own = Patient.objects.get(account=actor)
    client.post(f"/patients/{own.pk}/select/")
    saved = client.post(url, data)
    assert saved.status_code == 302
    assert parse_qs(urlsplit(saved.url).query)["patient"] == [str(patient.pk)]
    lesion = Lesion.objects.get()
    assert lesion.created_by_id == actor.pk
    operation = LesionOperation.objects.get(action="MATCH")
    assert operation.author_id == actor.pk and operation.observation_revisions.count() == 2
    detail = client.get(f"/lesions/{lesion.pk}/")
    assert detail.status_code == 200 and detail.context["request"].patient.pk == patient.pk
    assert "观察 &lt;script&gt;A&lt;/script&gt;" in detail.content.decode()
    assert "<script>A</script>" not in detail.content.decode()
    assert len(detail.context["measurements"]) == 2


def test_proposal_old_preview_cannot_confirm_after_source_revoke(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    _, patient, client, actor, _ = family_pair(django_user_model, "lesion-http-source")
    generate_proposals(patient, actor=actor)
    proposal = LesionMatchProposal.objects.get()
    url = f"/lesions/proposals/{proposal.pk}/"
    opened = client.get(url)
    assert opened.status_code == 200
    data = submitted_form(opened, patient, action="CONFIRM", name="观察 A", checked_original="on")
    row = review_observations(patient, actor=actor)[0]
    field = Fact.objects.get(pk=next(field["id"] for field in row["fields"] if field["field_key"] == "lesion.site"))
    revise_fact(patient, field.pk, actor=actor, action="REVOKE", expected_revision=field.revision_number,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True, **review_parent_arguments(field))
    result = client.post(url, data)
    assert result.status_code == 409 and "来源" in result.content.decode()
    assert not Lesion.objects.exists()


def test_viewer_read_foreign_scope_and_audit_use_actual_proposal_patient(django_user_model):
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient, viewer, actor, _ = family_pair(django_user_model, "lesion-http-viewer", "VIEWER")
    generate_proposals(patient, actor=patient.account)
    proposal = LesionMatchProposal.objects.get()
    url = f"/lesions/proposals/{proposal.pk}/"
    response = viewer.get(url)
    assert response.status_code == 200 and 'name="action"' not in response.content.decode()
    assert viewer.post("/lesions/propose/", {"patient_id": str(patient.pk)}).status_code == 403
    assert viewer.post(url, {"patient_id": str(patient.pk), "action": "REJECT"}).status_code == 403
    own = Patient.objects.get(account=actor)
    assert viewer.get(url, {"patient": str(own.pk)}).status_code == 404
    event = AuditEvent.objects.filter(action="lesion_viewed", target_hash=_hash("target", proposal.pk)).latest("created_at")
    assert event.result == "denied" and event.patient_hash == _hash("patient", patient.pk)
    assert event.resource_type == "lesion_proposal" and event.actor_hash == _hash("actor", actor.pk)


def test_manual_create_match_rename_split_unlink_and_undo_are_available_with_source_review(django_user_model):
    _, patient, client, actor, _ = family_pair(django_user_model, "lesion-http-manual")
    rows = review_observations(patient, actor=actor)
    first, second = rows
    source_url = f'/lesions/reports/{first["report_id"]}/{first["entity_key"]}/'
    opened = client.get(source_url)
    assert opened.status_code == 200 and 'title="观察来源原件"' in opened.content.decode()
    created = client.post(source_url, submitted_form(opened, patient, name="人工观察 A", checked_original="on"))
    assert created.status_code == 302 and Lesion.objects.count() == 1
    lesion = Lesion.objects.get()
    match_url = "/lesions/match/"
    match_page = client.get(match_url, {"patient": str(patient.pk), "first_id": first["id"], "second_id": second["id"]})
    assert match_page.status_code == 200
    matched = client.post(match_url, submitted_form(match_page, patient, action="CONFIRM", checked_original="on"))
    assert matched.status_code == 302 and all(row["usable"] for row in review_observations(patient, actor=actor))
    lesion.refresh_from_db()
    renamed = client.post(f"/lesions/{lesion.pk}/rename/", {"patient_id": str(patient.pk),
                          "expected_revision": lesion.revision_number, "name": "稳定观察 A"})
    assert renamed.status_code == 302 and lesion.original_name == "人工观察 A"
    manage_url = f"/lesions/{lesion.pk}/observations/"
    split_page = client.get(manage_url)
    assert split_page.status_code == 200
    split = client.post(manage_url, submitted_form(split_page, patient, action="SPLIT", name="独立观察 B",
                                                selected=[second["id"]], checked_original="on"))
    assert split.status_code == 302 and Lesion.objects.count() == 2
    operation = LesionOperation.objects.get(action="SPLIT")
    operation_url = f"/lesions/operations/{operation.pk}/"
    assert client.get(operation_url).status_code == 200
    assert client.post(operation_url + "undo/", {"patient_id": str(patient.pk)}).status_code == 302
    assert {row["lesion_id"] for row in review_observations(patient, actor=actor)} == {str(lesion.pk)}
    unlink_page = client.get(manage_url)
    unlinked = client.post(manage_url, submitted_form(unlink_page, patient, action="UNLINK", selected=[second["id"]]))
    assert unlinked.status_code == 302
    current = next(row for row in review_observations(patient, actor=actor) if row["id"] == second["id"])
    assert current["lesion_id"] is None and current["status"] == "UNASSIGNED"
    assert LesionOperation.objects.get(action="UNLINK").author_id == actor.pk


def test_manual_match_navigation_keeps_empty_selection_unbound_and_partial_selection_invalid(django_user_model):
    _, patient, client, actor, _ = family_pair(django_user_model, "lesion-http-selection")
    opened = client.get("/lesions/match/", {"patient": str(patient.pk)})
    assert opened.status_code == 200 and not opened.context["form"].is_bound
    assert not opened.context["form"].errors and not LesionOperation.objects.exists()
    first = review_observations(patient, actor=actor)[0]
    partial = client.get("/lesions/match/", {"patient": str(patient.pk), "first_id": first["id"]})
    assert partial.status_code == 400 and "second_id" in partial.context["form"].errors
    assert not LesionOperation.objects.exists()


def test_read_rechecks_revocation_after_render_without_leaking_observation_body(django_user_model, monkeypatch):
    from django.utils import timezone
    from apps.lesions import views
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient, client, actor, member = family_pair(django_user_model, "lesion-http-revoke")
    generate_proposals(patient, actor=actor)
    proposal = LesionMatchProposal.objects.get()
    original_render = views.render
    def revoked(*args, **kwargs):
        response = original_render(*args, **kwargs)
        type(member).objects.filter(pk=member.pk).update(revoked_at=timezone.now())
        return response
    monkeypatch.setattr(views, "render", revoked)
    response = client.get(f"/lesions/proposals/{proposal.pk}/")
    assert response.status_code in {403, 404} and "12mm" not in response.content.decode()
    event = AuditEvent.objects.filter(action="lesion_viewed", target_hash=_hash("target", proposal.pk)).latest("created_at")
    assert event.result == "denied" and event.patient_hash == _hash("patient", patient.pk)


def test_current_dimension_and_suv_charts_expose_conversion_sources_and_explicit_maximum_scope(django_user_model):
    from apps.lesions.services import match_observations
    from .test_relationships import expectations

    _, patient, client, actor, _ = family(django_user_model, "lesion-http-charts")
    imaging_observation(django_user_model, patient=patient, size="1.2", unit="cm", suv="3.2",
                        impression="本次最大病灶为左肺上叶结节。")
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15", suv="4.1",
                        impression="本次最大病灶为左肺上叶结节。")
    rows = review_observations(patient, actor=actor)
    assert len(rows) == 2
    match_observations(patient, actor=actor, first_id=rows[0]["id"], second_id=rows[1]["id"],
                       expectations=expectations(rows), checked_original=True, name="观察 A")
    lesion = Lesion.objects.get()
    detail = client.get(f"/lesions/{lesion.pk}/")
    assert detail.status_code == 200 and len(detail.context["charts"]) == 2
    assert all(len(chart["segments"]) == 1 for chart in detail.context["charts"])
    body = detail.content.decode()
    assert "1.2 cm × 10" in body and "12 mm" in body and "尺寸" in body and "SUVmax" in body
    assert body.count('class="lesion-trend"') == 2 and 'class="lesion-trend-segment"' in body
    maximum = client.get("/lesions/maximum/", {"patient": str(patient.pk)})
    assert maximum.status_code == 200 and len(maximum.context["measurements"]) == 4
    assert all(point.report_maximum for point in maximum.context["measurements"])
    assert "不从不完整候选中计算最大值" in maximum.content.decode()
