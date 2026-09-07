from urllib.parse import parse_qs, urlsplit

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import SnapshotChanged
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_clinical_exports import _all_structured_payloads
from tests.facts.test_imaging_quantitative import fields, imaging
from tests.facts.test_imaging_quantitative_views import scalar_post
from tests.patients.test_family_shares import ShareLink, exchange


pytestmark = pytest.mark.django_db


def test_selected_suv_keeps_numeric_contract_in_card_search_csv_json_zip_and_http_share(django_user_model):
    from apps.exports.pdf import card_sections
    from apps.patients.models import PatientShare

    owner, patient, document, _, _ = imaging(django_user_model,
        "左肺上叶结节，UNSELECTED_BODY_CANARY，约987.654mm，SUVmax≤4.20。右肺下叶结节约8mm，SUVmax7.7。",
        impression="对比前片：左肺结节同前。UNSELECTED_IMPRESSION_CANARY。")
    selected = fields(document, "lesion.suvmax")[0]
    assert build_snapshot(patient, {"mode": "all"})["clinical_fields"] == []
    assert owner.post(f"/facts/{selected.pk}/", scalar_post(patient, selected)).status_code == 302
    assert owner.get("/records/", {"q": "4.20"}).context["page_obj"].paginator.count == 1
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)],
                                        "clinical_field_ids": [str(selected.pk)], "details": True})
    assert len(snapshot["clinical_fields"]) == 1
    value = snapshot["clinical_fields"][0]["content"]["value"]
    assert value["values"] == ["4.20"] and value["comparator"] == "LE" and value["unit"] is None
    payload, _ = _all_structured_payloads(snapshot)
    assert "4.20" in payload
    assert "UNSELECTED_BODY_CANARY" not in payload and "UNSELECTED_IMPRESSION_CANARY" not in payload
    assert "987.654" not in payload
    assert any("4.20" in row["text"] for section in card_sections(snapshot) for row in section["entries"])
    response = owner.post(f"/patients/{patient.pk}/shares/", {"document_ids": [str(document.pk)], "sections": ["imaging"],
        "clinical_field_ids": [str(selected.pk)], "expires_in_hours": 24})
    assert response.status_code == 201
    share = PatientShare.objects.get(patient=patient)
    assert "UNSELECTED_BODY_CANARY" not in repr(share.snapshot)
    assert [field["id"] for field in share.snapshot["clinical_fields"]] == [str(selected.pk)]
    parser = ShareLink()
    parser.feed(response.content.decode())
    token = parse_qs(urlsplit(parser.value).fragment)["token"][0]
    viewer, _ = _patient(django_user_model, "quantitative-recipient")
    share_id = exchange(viewer, token)
    page = viewer.get(f"/shared/{share_id}/")
    assert page.status_code == 200 and "4.20" in page.content.decode()
    assert "UNSELECTED_BODY_CANARY" not in page.content.decode()
    assert viewer.get(f"/shared/{share_id}/documents/{document.pk}/").status_code == 404
    assert viewer.get(f"/facts/{selected.pk}/").status_code == 404


@pytest.mark.parametrize("change", ["revoke", "parent", "reparse"])
def test_new_fields_lose_export_and_share_after_source_or_review_changes(django_user_model, change):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import revise_report
    from apps.patients.sharing import create_share
    from tests.facts.test_clinical_foundation import clinical_fixture

    _, patient, document, version, _ = imaging(django_user_model,
        "对比前片（2025年06月）：双肺结节，较大者位于左肺，约12mm，SUVmax3.3。",
        name="quantitative-invalidation-" + change)
    all_fields = [f for key in ("lesion.suvmax", "lesion.maximum_scope", "comparison.statement", "comparison.reference_date") for f in fields(document, key)]
    for field in all_fields:
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "clinical_field_ids": [str(f.pk) for f in all_fields]})
    assert len(snapshot["clinical_fields"]) == 4
    result = create_share(patient, patient.account, {"document_ids": [str(document.pk)], "sections": ["imaging"],
                                                   "clinical_field_ids": [str(f.pk) for f in all_fields]})
    viewer, _ = _patient(django_user_model, "quantitative-stale-viewer-" + change)
    share_id = exchange(viewer, result.token)
    assert viewer.get(f"/shared/{share_id}/").status_code == 200
    if change == "revoke":
        field = all_fields[0]
        field.refresh_from_db()
        revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                    expected_source=effective_fact(field)["current_source_token"])
    elif change == "parent":
        report = document.clinical_reports.get()
        revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=0,
                      expected_source=report_source_token(report))
    else:
        clinical_fixture(django_user_model, document=document, previous=version)
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    response = viewer.get(f"/shared/{share_id}/")
    assert response.status_code == 410 and "SUVmax" not in response.content.decode()
    result.share.refresh_from_db()
    assert result.share.snapshot == {}


def test_a_reference_date_selected_alone_does_not_disclose_its_comparison_sentence(django_user_model):
    _, patient, document, _, _ = imaging(django_user_model,
        "对比前片（2025年06月）：左肺结节约12mm。", impression="较前缩小，COMPARISON_PRIVATE_CANARY。")
    date = fields(document, "comparison.reference_date")[0]
    revise_fact(patient, date.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(date)["current_source_token"], checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "clinical_field_ids": [str(date.pk)]})
    payload, _ = _all_structured_payloads(snapshot)
    assert "2025-06" in payload and "COMPARISON_PRIVATE_CANARY" not in payload and "12mm" not in payload
    assert all(f["field_key"] == "comparison.reference_date" for f in snapshot["clinical_fields"])
