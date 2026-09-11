import pytest

from tests.facts.molecular_factories import graph
from tests.facts.test_pathology_views import submitted, review_post

pytestmark = pytest.mark.django_db


def test_real_report_and_field_routes_use_molecular_controls_and_preserve_source(django_user_model):
    client, patient, _, report, fields = graph(django_user_model)
    page = client.get(f"/facts/reports/{report.pk}/", {"field_key": "variant.identity"})
    assert page.status_code == 200
    keys = dict(page.context["field_keys"])
    assert {"variant.identity", "drug_evidence.level", "assay.negative_statement", "ihc.score", "assay.report_date"} <= keys.keys()
    assert "lesion.dimensions" not in keys and "pathology.diagnosis" not in keys
    for key in ("specimen", "assay", "identity", "metric"):
        response = review_post(client, patient, fields[key])
        assert response.status_code == 302, response.content.decode()
    page = client.get(f"/facts/{fields['identity'].pk}/")
    assert page.status_code == 200
    assert 'name="transcripts_raw"' in page.content.decode()
    assert 'name="partner_1_gene_raw"' in page.content.decode()
    assert fields["identity"].automatic_content["value"]["transcripts"]["values"][0] in page.content.decode()


def test_manual_unlinked_molecular_quantity_remains_pending_then_confirmed_but_unusable(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    client, patient, _, report, _ = graph(django_user_model)
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url, {"field_key": "assay.tmb_value"})
    assert page.status_code == 200
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), action="manual_field", field_key="assay.tmb_value", raw_value="SYN TMB 01.20 mut/Mb",
        page_number=1, source_role="CURRENT_RESULT", status="PARSED", scalar_1="01.20", comparator="EQ", unit="mut/Mb",
        unit_state="PRINTED", quantity_assertion="AS_REPORTED_NO_POSITIVITY_INFERRED", value_raw="01.20 mut/Mb")
    saved = client.post(url, data)
    assert saved.status_code == 302, saved.content.decode()
    field = report.fields.get(raw_text="SYN TMB 01.20 mut/Mb")
    assert effective_field(field)["status"] == "PENDING"
    assert review_post(client, patient, field).status_code == 302
    assert effective_field(field)["status"] == "CONFIRMED" and not effective_field(field)["usable"]


@pytest.mark.parametrize("omit_drug", [False, True])
def test_group_http_preserves_full_ordered_drug_users_and_whole_undo(django_user_model, omit_drug):
    from django.test import Client
    from apps.facts.clinical_readmodels import effective_field
    from tests.facts.test_molecular_context import drug_graph, confirm
    patient, _, report, fields, _, _, _ = drug_graph(django_user_model)
    client = Client()
    client.force_login(patient.account)
    confirm(patient, fields)
    url = f"/facts/{fields['identity'].pk}/"
    page = client.get(url, {"edit_context": "1"})
    entries = page.context["replacement_forms"]
    assert {e["field"].pk for e in entries} == {fields[k].pk for k in ("identity", "metric", "drugs")}
    data = submitted(page.context["context_action_form"])
    data.update(patient_id=str(patient.pk), action="REPLACE_CONTEXT", checked_original="on")
    for entry in entries:
        if not omit_drug or entry["field"].field_key != "drug_evidence.drugs":
            data.update(submitted(entry["form"]))
    before = set(report.fields.values_list("pk", flat=True))
    response = client.post(url, data)
    if omit_drug:
        assert response.status_code == 400
        assert set(report.fields.values_list("pk", flat=True)) == before
        assert not fields["identity"].revisions.filter(action="REPLACE_CONTEXT").exists()
        return
    assert response.status_code == 302, response.content.decode()
    new = list(report.fields.exclude(pk__in=before))
    assert len(new) == 3 and all(effective_field(f)["status"] == "PENDING" for f in new)
    drugs = next(f for f in new if f.field_key == "drug_evidence.drugs")
    targets = [b["target_fact_id"] for b in drugs.automatic_content["entity_context"]["bindings"] if b["role"] == "VARIANT"]
    identity = next(f for f in new if f.field_key == "variant.identity")
    assert targets == [str(identity.pk), str(fields["second"].pk)]
    page = client.get(url)
    undo = submitted(page.context["context_action_form"])
    undo.update(patient_id=str(patient.pk), action="UNDO_CONTEXT")
    assert client.post(url, undo).status_code == 302
    assert all(effective_field(f)["status"] == "EXCLUDED" for f in new)
    assert all(effective_field(fields[k])["status"] == "PENDING" for k in ("identity", "metric", "drugs"))


@pytest.mark.parametrize("route", ["field", "report", "reports"])
def test_molecular_render_guard_discards_changed_source(django_user_model, monkeypatch, route):
    from apps.facts import views
    from tests.facts.test_molecular_context import confirm
    from tests.facts.pathology_factories import review
    client, patient, document, report, fields = graph(django_user_model)
    confirm(patient, fields)
    original = views.render
    def change(*args, **kwargs):
        response = original(*args, **kwargs)
        review(patient, fields["assay"], "REVOKE")
        return response
    monkeypatch.setattr(views, "render", change)
    url = {"field": f"/facts/{fields['metric'].pk}/", "report": f"/facts/reports/{report.pk}/", "reports": f"/facts/documents/{document.pk}/reports/"}[route]
    response = client.get(url)
    assert response.status_code == 410
    assert "01.20" not in response.content.decode()


def test_molecular_boundary_http_reextracts_actual_parser_and_undo_preserves_old_candidates(django_user_model):
    from copy import deepcopy
    from apps.facts.clinical_readmodels import effective_field
    from tests.facts.test_molecular_pipeline import fixture, report_rows
    patient, document, version, _ = fixture(django_user_model, report_rows())
    from django.test import Client
    client = Client()
    client.force_login(patient.account)
    report = document.clinical_reports.get()
    originals = {f.pk: deepcopy(f.automatic_content) for f in report.fields.all()}
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url)
    data = submitted(page.context["boundary_form"])
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    data.update(patient_id=str(patient.pk), action="REPLACE", mode="ocr", first_ocr_block=str(blocks[0].pk), last_ocr_block=str(blocks[-1].pk))
    result = client.post(url, data)
    assert result.status_code == 302, result.content.decode()
    new = document.clinical_reports.exclude(pk=report.pk).get()
    assert new.routing_kind == "MOLECULAR" and new.schema_version == "MOLECULAR_REPORT_V1"
    assert new.fields.filter(field_key="variant.identity").count() == report.fields.filter(field_key="variant.identity").count() > 0
    assert {f.pk: f.automatic_content for f in report.fields.all()} == originals
    assert all(effective_field(f)["status"] == "PENDING" for f in new.fields.all())
    data = submitted(client.get(url).context["action_form"])
    data.update(patient_id=str(patient.pk), action="UNDO")
    assert client.post(url, data).status_code == 302
    assert all(effective_field(f)["status"] == "PENDING" for f in report.fields.all())
    assert all(effective_field(f)["status"] == "EXCLUDED" for f in new.fields.all())
