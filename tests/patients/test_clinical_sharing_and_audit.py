"""Persisted clinical records obey the merged sharing and patient audit contracts."""
from apps.facts.laterality import review_parent_arguments

from urllib.parse import parse_qs, urlsplit

import pytest

from apps.facts.clinical_readmodels import report_source_token
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import add_manual_fact, revise_fact
from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import PatientMembership, PatientShare
from tests.documents.test_detail_viewer import _patient
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.patients.test_family_shares import ShareLink, exchange


pytestmark = pytest.mark.django_db


def shared_clinical_fixture(django_user_model, name):
    texts = [
        "CT诊断报告书\n检查日期：2026-08-17\n影像所见：左肺上叶见结节，约12mm。\n诊断意见：SELECTED_REPORT_IMPRESSION。",
        "MR诊断报告书\n检查日期：2026-08-18\n影像所见：右额叶见结节，约8mm。\n诊断意见：OTHER_REPORT_CANARY。",
    ]
    owner, patient, document, version, _ = clinical_fixture(django_user_model, texts=texts, name=name)
    reports = list(document.clinical_reports.order_by("ordinal"))
    for field in version.facts.filter(representation="FIELD"):
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(field)["current_source_token"], checked_original=True, **review_parent_arguments(field))
    legacy = add_manual_fact(patient, document.pk, page_number=1, category="IMAGING",
                             text="UNSELECTED_EXCERPT_CANARY", actor=patient.account)
    revise_fact(patient, legacy.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(legacy)["current_source_token"], checked_original=True, **review_parent_arguments(legacy))
    return owner, patient, document, version, reports


def test_clinical_reads_and_denied_mutations_audit_the_resource_patient_and_actual_actor(django_user_model):
    owner, patient, document, _, _ = clinical_fixture(django_user_model, name="clinical-audit-owner")
    report = document.clinical_reports.get()
    reader, own = _patient(django_user_model, "clinical-audit-reader")
    outsider, foreign = _patient(django_user_model, "clinical-audit-outsider")
    PatientMembership.objects.create(patient=patient, account=own.account, role="VIEWER")
    url = f"/facts/reports/{report.pk}/"
    assert reader.get(url).status_code == 200
    assert outsider.get(url).status_code == 404
    assert reader.post(url, {"patient_id": str(patient.pk), "action": "EXCLUDE", "expected_revision": 0,
                             "expected_source": report_source_token(report)}).status_code == 403
    reads = AuditEvent.objects.filter(route_name="facts:report", action="clinical_report_viewed")
    assert reads.filter(actor_hash=_hash("actor", own.account_id), patient_hash=_hash("patient", patient.pk),
                        target_hash=_hash("target", report.pk), resource_type="clinical_report", result="succeeded").count() == 1
    assert reads.filter(actor_hash=_hash("actor", foreign.account_id), patient_hash=_hash("patient", patient.pk),
                        target_hash=_hash("target", report.pk), resource_type="clinical_report", result="denied").count() == 1
    assert AuditEvent.objects.filter(route_name="facts:report", action="clinical_report_revised", result="denied",
                                    actor_hash=_hash("actor", own.account_id), patient_hash=_hash("patient", patient.pk),
                                    target_hash=_hash("target", report.pk), resource_type="clinical_report").count() == 1
    response = owner.get(f"/patients/{patient.pk}/audit/", {"resource_type": "clinical_report"})
    assert response.status_code == 200 and "查看结构化报告" in response.content.decode()


@pytest.mark.parametrize("selection_kind", ["field", "report"])
def test_http_share_selects_persisted_fields_without_other_report_or_excerpt_content(django_user_model, selection_kind):
    owner, patient, document, _, reports = shared_clinical_fixture(django_user_model, "clinical-share-" + selection_kind)
    field = reports[0].fields.get(field_key="lesion.site")
    key, identity = ("clinical_field_ids", field.pk) if selection_kind == "field" else ("report_ids", reports[0].pk)
    response = owner.post(f"/patients/{patient.pk}/shares/", {
        "document_ids": [str(document.pk)], "sections": ["imaging"], key: [str(identity)], "expires_in_hours": 24,
    })
    assert response.status_code == 201
    share = PatientShare.objects.get(patient=patient)
    assert share.scope[key] == [str(identity)]
    assert [row["id"] for row in share.snapshot["clinical_reports"]] == [str(reports[0].pk)]
    public = repr(share.snapshot)
    assert "OTHER_REPORT_CANARY" not in public and "UNSELECTED_EXCERPT_CANARY" not in public
    assert share.snapshot["facts"] == [] and share.snapshot["labs"] == []
    assert all("raw_value" not in row["content"] and "transformations" not in row["content"]
               for row in share.snapshot["clinical_fields"])
    if selection_kind == "field":
        assert [row["id"] for row in share.snapshot["clinical_fields"]] == [str(field.pk)]
        assert "12mm" not in public and "SELECTED_REPORT_IMPRESSION" not in public
    parser = ShareLink()
    parser.feed(response.content.decode())
    token = parse_qs(urlsplit(parser.value).fragment)["token"][0]
    viewer, _ = _patient(django_user_model, "clinical-share-recipient-" + selection_kind)
    share_id = exchange(viewer, token)
    page = viewer.get(f"/shared/{share_id}/")
    assert page.status_code == 200 and "左肺上叶" in page.content.decode()
    assert "OTHER_REPORT_CANARY" not in page.content.decode() and "UNSELECTED_EXCERPT_CANARY" not in page.content.decode()
    assert viewer.get(f"/shared/{share_id}/documents/{document.pk}/").status_code == 404
    assert viewer.get(f"/shared/{share_id}/documents/{document.pk}/original/").status_code == 404
    assert viewer.get(f"/facts/{field.pk}/").status_code == 404


@pytest.mark.parametrize("change", ["field", "parent", "parse", "new_report"])
def test_typed_share_stops_after_clinical_revision_or_source_scope_changes(django_user_model, change):
    from apps.facts.clinical_services import create_manual_report, revise_report
    from apps.patients.sharing import create_share

    _, patient, document, version, reports = shared_clinical_fixture(django_user_model, "clinical-share-change-" + change)
    field = reports[0].fields.get(field_key="lesion.site")
    result = create_share(patient, patient.account, {"document_ids": [str(document.pk)], "sections": ["imaging"],
                                                   "clinical_field_ids": [str(field.pk)]})
    viewer, _ = _patient(django_user_model, "clinical-share-change-reader-" + change)
    share_id = exchange(viewer, result.token)
    assert viewer.get(f"/shared/{share_id}/").status_code == 200
    if change == "field":
        revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                    expected_source=effective_fact(field)["current_source_token"], **review_parent_arguments(field))
    elif change == "parent":
        revise_report(patient, actor=patient.account, report_id=reports[0].pk, action="EXCLUDE", expected_revision=0,
                      expected_source=report_source_token(reports[0]))
        event = AuditEvent.objects.get(action="clinical_report_revised", target_hash=_hash("target", reports[0].pk))
        assert event.patient_hash == _hash("patient", patient.pk) and event.resource_type == "clinical_report"
    elif change == "parse":
        clinical_fixture(django_user_model, document=document, previous=version)
    else:
        create_manual_report(patient, actor=patient.account, document_id=document.pk, spans=[{"page_number": 1}],
                             title="新增人工报告范围", expected_lifecycle_revision=document.lifecycle_revision,
                             expected_version_id=version.pk)
    response = viewer.get(f"/shared/{share_id}/")
    assert response.status_code == 410 and "左肺上叶" not in response.content.decode()
    result.share.refresh_from_db()
    assert result.share.snapshot == {} and result.share.invalidated_at is not None


@pytest.mark.parametrize("invalid", ["sources", "pending", "foreign_field"])
def test_http_typed_share_rejects_full_sources_and_unusable_or_foreign_fields(django_user_model, invalid):
    owner, patient, document, _, reports = shared_clinical_fixture(django_user_model, "clinical-share-invalid-" + invalid)
    field = reports[0].fields.get(field_key="lesion.site")
    sections = ["imaging"]
    if invalid == "sources":
        sections.append("sources")
    elif invalid == "pending":
        revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                    expected_source=effective_fact(field)["current_source_token"], **review_parent_arguments(field))
    else:
        _, _, other, _, _ = clinical_fixture(django_user_model, name="foreign-clinical-share")
        field = other.facts.get(field_key="imaging.impression")
    response = owner.post(f"/patients/{patient.pk}/shares/", {"document_ids": [str(document.pk)], "sections": sections,
                                                           "clinical_field_ids": [str(field.pk)]})
    assert response.status_code == 400 and not PatientShare.objects.filter(patient=patient).exists()


def test_unchecked_imaging_section_never_releases_persisted_selected_report_fields(django_user_model):
    from apps.patients.sharing import create_share

    _, patient, document, _, reports = shared_clinical_fixture(django_user_model, "clinical-share-sections")
    result = create_share(patient, patient.account, {"document_ids": [str(document.pk)], "sections": ["labs"],
                                                   "report_ids": [str(reports[0].pk)]})
    assert result.share.snapshot["clinical_reports"] == result.share.snapshot["clinical_fields"] == result.share.snapshot["clinical_field_sources"] == []
    assert "SELECTED_REPORT_IMPRESSION" not in repr(result.share.snapshot)


def test_clinical_service_actions_keep_actual_editor_and_patient_without_medical_audit_text(django_user_model):
    from apps.facts.clinical_services import add_manual_clinical_field

    _, patient, document, version, _ = clinical_fixture(django_user_model, name="clinical-audit-service-owner")
    editor, own = _patient(django_user_model, "clinical-audit-service-editor")
    PatientMembership.objects.create(patient=patient, account=own.account, role="EDITOR")
    url = f"/facts/documents/{document.pk}/reports/"
    assert editor.post(url, {"patient_id": str(patient.pk), "action": "extract",
                             "expected_version_id": str(version.pk)}).status_code == 302
    assert editor.post(url, {"patient_id": str(patient.pk), "title": "合成人工报告", "first_page": 1, "last_page": 1,
                             "expected_lifecycle_revision": document.lifecycle_revision,
                             "expected_version_id": str(version.pk)}).status_code == 302
    report = document.clinical_reports.get(origin="MANUAL")
    fact = add_manual_clinical_field(patient, actor=own.account, report_id=report.pk, entity_key="report",
                                    field_key="imaging.impression", value={"text": "AUDIT_MEDICAL_CANARY"},
                                    fragments=[{"page_number": 1, "raw_text": "AUDIT_MEDICAL_CANARY"}],
                                    expected_report_source=report_source_token(report))
    for action, target, kind in [("clinical_extraction_requested", document, "document"),
                                 ("clinical_report_added", report, "clinical_report"),
                                 ("clinical_field_added", fact, "fact")]:
        event = AuditEvent.objects.get(action=action, target_hash=_hash("target", target.pk))
        assert event.actor_hash == _hash("actor", own.account_id) and event.patient_hash == _hash("patient", patient.pk)
        assert event.resource_type == kind
        assert "AUDIT_MEDICAL_CANARY" not in repr(AuditEvent.objects.values().get(pk=event.pk))
