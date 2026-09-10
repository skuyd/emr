import pytest

from apps.facts.clinical_readmodels import effective_field, report_source_token
from apps.facts.clinical_services import create_manual_report, add_manual_clinical_field
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.molecular_factories import context_for
from tests.facts.test_molecular_contracts import quantity
from tests.facts.test_pathology_views import submitted

pytestmark = pytest.mark.django_db


def test_group_form_roundtrip_retains_multipage_original_order_and_private_count(django_user_model):
    client, patient = _patient(django_user_model, "molecular-multipage-http")
    document, _ = _document(patient, page_count=2, status="PROCESSING_FAILED")
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk, title="合成跨页分子报告",
        spans=[{"page_number": 1}, {"page_number": 2}], expected_lifecycle_revision=0, expected_version_id=None, routing_kind="MOLECULAR")
    originals = [{"page_number": 2, "raw_text": "01.20 mut/Mb；result:"}, {"page_number": 1, "raw_text": "negative"}]
    fact = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, field_key="assay.tmb_value", entity_key="assay:a",
        value=quantity("TMB", unit="mut/Mb"), fragments=originals, expected_report_source=report_source_token(report),
        entity_context=context_for(report, {"SPECIMEN": None, "ASSAY": None}), source_role="CURRENT_RESULT", own_fragment_count=2,
        reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [0, 1]})
    url = f"/facts/{fact.pk}/"
    page = client.get(url, {"edit_context": "1"})
    form = page.context["replacement_forms"][0]["form"]
    assert form.initial["page_number"] == 2
    assert form.initial["supplemental_0_page"] == 1
    assert form.initial["raw_value"] == originals[0]["raw_text"]
    assert form.initial["supplemental_0_text"] == originals[1]["raw_text"]
    data = submitted(page.context["context_action_form"])
    data.update(submitted(form))
    data.update(patient_id=str(patient.pk), action="REPLACE_CONTEXT", checked_original="on")
    response = client.post(url, data)
    assert response.status_code == 302, response.content.decode()
    new = report.fields.exclude(pk=fact.pk).get()
    assert [{"page_number": p.document_page.page_number, "raw_text": p.raw_text} for p in new.source_fragments.order_by("ordinal")] == originals
    assert new.automatic_content["manual_source"]["own_fragment_count"] == 2
    assert effective_field(new)["status"] == "PENDING" and not effective_field(new)["usable"]


def test_report_manual_form_cannot_bind_foreign_patient_variant(django_user_model):
    from tests.facts.molecular_factories import graph
    client, patient, _, report, _ = graph(django_user_model, "molecular-proof-http-owner")
    _, _, _, _, foreign = graph(django_user_model, "molecular-proof-http-foreign")
    url = f"/facts/reports/{report.pk}/"
    page = client.get(url, {"field_key": "variant.allele_fraction"})
    data = submitted(page.context["form"])
    data.update(patient_id=str(patient.pk), action="manual_field", field_key="variant.allele_fraction", raw_value="SYN 1%", page_number=1,
        value_raw="1%", status="PARSED", scalar_1="1", comparator="EQ", unit="%", unit_state="PRINTED", binding_variant=str(foreign["identity"].pk))
    before = report.fields.count()
    assert client.post(url, data).status_code == 400
    assert report.fields.count() == before


def test_viewer_routes_and_revoked_membership_preserve_patient_scope(django_user_model):
    from apps.patients.access import change_membership
    from apps.patients.models import PatientMembership
    from tests.facts.molecular_factories import graph
    _, patient, document, report, fields = graph(django_user_model)
    client, other = _patient(django_user_model, "molecular-viewer-http")
    member = PatientMembership.objects.create(patient=patient, account=other.account, role="VIEWER")
    urls = [f"/facts/{fields['metric'].pk}/", f"/facts/reports/{report.pk}/", f"/facts/documents/{document.pk}/reports/"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200 and 'name="action"' not in response.content.decode()
        assert client.get(url, {"patient": str(other.pk)}).status_code == 404
        assert client.post(url, {"patient_id": str(patient.pk), "action": "REPLACE_CONTEXT"}).status_code == 403
    change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
    assert all(client.get(url).status_code == 404 for url in urls)
