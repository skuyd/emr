from copy import deepcopy

import pytest

from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from tests.facts.pathology_factories import add_field, ihc_fixture, review, score_value


pytestmark = pytest.mark.django_db


def submitted(form):
    return {form.add_prefix(key): value for key, value in form.initial.items() if key in form.fields and value is not False and value is not None}


def review_post(client, patient, field, *, action="CONFIRM", updates=None):
    url = f"/facts/{field.pk}/"
    page = client.get(url)
    assert page.status_code == 200
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), action=action, checked_original="on")
    data.update(updates or {})
    return client.post(url, data)


def test_pathology_report_creation_and_field_choices_do_not_use_imaging_defaults(django_user_model):
    client, patient, document, report, fields = ihc_fixture(django_user_model, "pathology-http-kind")
    url = f"/facts/documents/{document.pk}/reports/"
    page = client.get(url)
    assert "routing_kind" in page.context["form"].fields
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), title="合成第二份病理", routing_kind="PATHOLOGY", first_page=1, last_page=1)
    saved = client.post(url, data)
    assert saved.status_code == 302
    new = document.clinical_reports.get(title="合成第二份病理")
    assert new.routing_kind == "PATHOLOGY"
    assert new.schema_version == "PATHOLOGY_IHC_V1"
    page = client.get(f"/facts/reports/{report.pk}/", {"field_key": "ihc.score"})
    assert page.status_code == 200
    keys = dict(page.context["field_keys"])
    assert "ihc.score" in keys and "assay.report_date" in keys and "lesion.dimensions" not in keys
    assert client.get(f"/facts/reports/{report.pk}/", {"field_key": "lesion.site"}).status_code == 404


def test_actual_http_graph_review_keeps_cps_unit_absence_and_author(django_user_model):
    client, patient, _, _, fields = ihc_fixture(django_user_model, "pathology-http-graph")
    for key in ("specimen", "assay", "clone", "marker", "tps", "cps"):
        field = fields[key]
        page = client.get(f"/facts/{field.pk}/")
        if key in {"tps", "cps"}:
            assert 'name="score_kind"' in page.content.decode()
            assert 'name="size_1"' not in page.content.decode()
        response = review_post(client, patient, field)
        assert response.status_code == 302, response.content.decode()
        field.refresh_from_db()
        assert field.revisions.get().author_id == patient.account_id
    row = effective_fact(fields["cps"])
    assert row["usable"] and row["content"]["value"]["unit"] is None
    assert row["content"]["value"]["score_kind"] == "CPS"
    assert "PD-L1" in client.get(f"/facts/{fields['cps'].pk}/").content.decode()
    old = submitted(client.get(f"/facts/{fields['cps'].pk}/").context["form"])
    old.update(patient_id=str(patient.pk), action="CONFIRM", checked_original="on")
    review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CLONE-B"}, "raw_value": "SYN-CLONE-B"})
    assert client.post(f"/facts/{fields['cps'].pk}/", old).status_code == 409
    assert not effective_fact(fields["cps"])["usable"]


def test_unknown_manual_score_is_retained_but_never_usable_after_text_confirmation(django_user_model):
    client, patient, _, report, _ = ihc_fixture(django_user_model, "pathology-http-unknown")
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url, {"field_key": "ihc.score"})
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), action="manual_field", field_key="ihc.score", raw_value="不明检测 CPS 21",
                score_kind="CPS", scalar_1="21", comparator="EQ", original_unit="", assertion="AS_REPORTED_NO_POSITIVITY_INFERRED",
                page_number=1, source_role="CURRENT_RESULT")
    for role in ("specimen", "assay", "marker"):
        data["binding_" + role] = "unknown:NOT_STATED"
    saved = client.post(url, data)
    assert saved.status_code == 302, saved.content.decode()
    field = report.fields.get(raw_text="不明检测 CPS 21")
    assert field.origin == "MANUAL" and field.source_fragments.count() == 1
    assert review_post(client, patient, field).status_code == 302
    field.refresh_from_db()
    row = effective_fact(field)
    assert row["status"] == "CONFIRMED" and row["context_state"] == "UNLINKED" and not row["usable"]
    body = client.get(f"/facts/{field.pk}/").content.decode()
    assert "未关联" in body


def test_manual_field_cannot_borrow_other_report_anchor_or_wrong_original_proof(django_user_model):
    client, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-http-proof")
    _, _, _, _, others = ihc_fixture(django_user_model, "pathology-http-other")
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url, {"field_key": "ihc.score"})
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), action="manual_field", field_key="ihc.score", raw_value="CPS 21",
                score_kind="CPS", scalar_1="21", comparator="EQ", original_unit="", assertion="AS_REPORTED_NO_POSITIVITY_INFERRED",
                page_number=1, source_role="CURRENT_RESULT")
    for role in ("specimen", "assay", "marker"):
        data.update({"binding_" + role: str(fields[role].pk), "proof_" + role: "不包含该锚身份", "proof_page_" + role: 1})
    count = report.fields.count()
    assert client.post(url, data).status_code == 400
    assert report.fields.count() == count
    data["binding_specimen"] = str(others["specimen"].pk)
    assert client.post(url, data).status_code == 400
    assert report.fields.count() == count


def test_marker_spelling_change_requires_group_ui_and_whole_group_undo_returns_pending(django_user_model):
    client, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-http-replace")
    assert review_post(client, patient, fields["marker"], action="CORRECT", updates={"identity_label": "Ki-67", "identity_raw": "Ki-67"}).status_code == 400
    url = f"/facts/{fields['marker'].pk}/"
    page = client.get(url, {"edit_context": "1"})
    assert page.status_code == 200 and len(page.context["replacement_forms"]) == 3
    data = submitted(page.context["context_action_form"])
    data.update(patient_id=str(patient.pk), action="REPLACE_CONTEXT", checked_original="on")
    for entry in page.context["replacement_forms"]:
        data.update(submitted(entry["form"]))
    # Keeping the identity is legal too: this still produces a new immutable
    # association whose scores must all be reviewed again.
    replaced = client.post(url, data)
    assert replaced.status_code == 302, replaced.content.decode()
    for key in ("marker", "tps", "cps"):
        fields[key].refresh_from_db()
        assert effective_fact(fields[key])["status"] == "EXCLUDED"
    new = report.fields.exclude(pk__in=[f.pk for f in fields.values()])
    assert new.count() == 3 and all(effective_fact(field)["status"] == "PENDING" for field in new)
    undo = client.get(url)
    values = submitted(undo.context["context_action_form"])
    values.update(patient_id=str(patient.pk), action="UNDO_CONTEXT")
    assert client.post(url, values).status_code == 302
    for key in ("marker", "tps", "cps"):
        fields[key].refresh_from_db()
        assert effective_fact(fields[key])["status"] == "PENDING"
    assert all(effective_fact(field)["status"] == "EXCLUDED" for field in new)


@pytest.mark.parametrize("route", ["field", "report", "reports"])
def test_context_changed_during_render_cannot_return_stale_confirmed_pathology(django_user_model, monkeypatch, route):
    from apps.facts import views
    from tests.facts.pathology_factories import confirm_graph

    client, patient, document, report, fields = ihc_fixture(django_user_model, "pathology-read-" + route)
    confirm_graph(patient, fields)
    original = views.render

    def change(*args, **kwargs):
        response = original(*args, **kwargs)
        review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CLONE-B"}, "raw_value": "SYN-CLONE-B"})
        return response

    monkeypatch.setattr(views, "render", change)
    url = {"field": f"/facts/{fields['tps'].pk}/", "report": f"/facts/reports/{report.pk}/", "reports": f"/facts/documents/{document.pk}/reports/"}[route]
    page = client.get(url)
    assert page.status_code == 410
    assert "TPS 13" not in page.content.decode() and "SYN-CLONE-A" not in page.content.decode()


def test_pathology_boundary_replacement_reextracts_same_kind_and_undo_keeps_old_text_pending(django_user_model):
    from tests.facts.test_pathology_pipeline import fixture

    client, patient, document, version, _ = fixture(django_user_model, name="pathology-http-boundary")
    report = document.clinical_reports.get()
    original_ids = set(report.fields.values_list("pk", flat=True))
    old_content = {field.pk: deepcopy(field.automatic_content) for field in report.fields.all()}
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url)
    values = submitted(page.context["boundary_form"])
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    values.update(patient_id=str(patient.pk), action="REPLACE", mode="ocr", first_ocr_block=str(blocks[0].pk), last_ocr_block=str(blocks[-1].pk))
    replaced = client.post(url, values)
    assert replaced.status_code == 302
    replacement = document.clinical_reports.exclude(pk=report.pk).get()
    assert replacement.routing_kind == "PATHOLOGY" and replacement.schema_version == "PATHOLOGY_IHC_V1"
    assert replacement.fields.filter(field_key="ihc.score").count() == 2
    assert all(effective_fact(field)["status"] == "PENDING" for field in replacement.fields.all())
    assert all(field.automatic_content == old_content[field.pk] for field in report.fields.all())
    page = client.get(url)
    values = submitted(page.context["action_form"])
    values.update(patient_id=str(patient.pk), action="UNDO")
    assert client.post(url, values).status_code == 302
    assert set(report.fields.values_list("pk", flat=True)) == original_ids
    assert all(effective_fact(field)["status"] == "PENDING" for field in report.fields.all())
    assert all(effective_fact(field)["status"] == "EXCLUDED" for field in replacement.fields.all())


def test_pathology_viewer_scope_and_post_authorization_survive_patient_tab_switch(django_user_model):
    from apps.patients.access import change_membership
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    _, patient, document, report, fields = ihc_fixture(django_user_model, "pathology-owner-tabs")
    client, other = _patient(django_user_model, "pathology-viewer-tabs")
    membership = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    routes = (f"/facts/{fields['tps'].pk}/", f"/facts/reports/{report.pk}/", f"/facts/documents/{document.pk}/reports/")
    for route in routes:
        page = client.get(route)
        assert page.status_code == 200 and 'name="action"' not in page.content.decode()
        assert client.get(route, {"patient": str(other.pk)}).status_code == 404
        assert client.post(route, {"patient_id": str(patient.pk), "action": "REPLACE_CONTEXT"}).status_code == 403
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    assert all(client.get(route).status_code == 404 for route in routes)


def test_group_replacement_does_not_pretend_mult_page_original_is_one_page(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import add_manual_clinical_field, create_manual_report
    from tests.documents.test_detail_viewer import _document, _patient
    from tests.facts.pathology_factories import context_for

    client, patient = _patient(django_user_model, "pathology-multi-page-replacement")
    document, _ = _document(patient, page_count=2, status="PROCESSING_FAILED")
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk, title="合成跨页病理",
                                  spans=[{"page_number": 1}, {"page_number": 2}], expected_lifecycle_revision=0,
                                  expected_version_id=None, routing_kind="PATHOLOGY")
    marker = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key="ihc:a", field_key="ihc.marker",
                                       value={"code": "PD_L1", "label": "PD-L1", "raw": "PD-L1"},
                                       fragments=[{"page_number": 1, "raw_text": "标记 PD-L1"}, {"page_number": 2, "raw_text": "跨页明确限定"}],
                                       entity_context=context_for(report, {"SPECIMEN": None, "ASSAY": None}), source_role="CURRENT_RESULT",
                                       expected_report_source=report_source_token(report))
    page = client.get(f"/facts/{marker.pk}/", {"edit_context": "1"})
    assert page.status_code == 200
    replacement_form = page.context["replacement_forms"][0]["form"]
    assert replacement_form.initial["raw_value"] == ""
    assert "跨页明确限定" in page.content.decode()
    # No silent replacement is possible before the reviewer supplies the new
    # value's actual page transcription instead of inheriting page one.
    values = submitted(page.context["context_action_form"])
    values.update(submitted(replacement_form))
    values.update(patient_id=str(patient.pk), action="REPLACE_CONTEXT", checked_original="on")
    assert client.post(f"/facts/{marker.pk}/", values).status_code == 400
    assert report.fields.count() == 1
