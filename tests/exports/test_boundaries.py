from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.exports.content import build_snapshot
from apps.exports.models import ExportJob
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from tests.documents.test_detail_viewer import _document, _patient
from tests.exports.test_jobs import _preview
from tests.facts.factories import parsed_facts
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_all_documents_snapshot_accepts_more_than_one_thousand_owned_documents(django_user_model):
    from apps.documents.models import Document

    _, patient = _patient(django_user_model, "visit-thousand")
    template, _ = _document(patient)
    Document.objects.bulk_create([
        Document(patient=patient, batch=template.batch, display_filename=f"synthetic-{index}.pdf",
                 content_type="application/pdf", byte_size=128, page_count=1,
                 sha256=f"{index:064x}", original_object_key=f"originals/synthetic-many/{index}")
        for index in range(1000)
    ])
    snapshot = build_snapshot(patient, {"mode": "all"})
    assert len(snapshot["documents"]) == 1001
    assert {item["id"] for item in snapshot["documents"]} == {
        str(pk) for pk in Document.objects.filter(patient=patient).values_list("pk", flat=True)
    }


@pytest.mark.parametrize("date_problem", ["uncertain", "conflicting"])
def test_uncertain_later_date_does_not_replace_latest_reliable_result(django_user_model, date_problem):
    _, patient = _patient(django_user_model, "visit-date-" + date_problem)
    _, earlier = _observation(patient, date(2026, 8, 1), "4")
    _, later = _observation(patient, date(2026, 8, 20), "5")
    candidates = later.parsing_version.metadata_candidates.filter(kind="DOCUMENT_DATE")
    candidates.update(**({"confidence": "0.80"} if date_problem == "uncertain"
                        else {"normalized_value": "2026-08-21"}))
    snapshot = build_snapshot(patient, {"mode": "all"})
    assert set(snapshot["card"]["lab_ids"]) == {str(earlier.pk), str(later.pk)}
    assert not snapshot["card"]["trends"]
    later_data = next(row for row in snapshot["labs"] if row["id"] == str(later.pk))
    assert {"date_uncertain" if date_problem == "uncertain" else "date_conflict"} <= {
        item["code"] for item in later_data["quality_issues"]
    }


@pytest.mark.parametrize("when", ["before_render", "during_render"])
def test_pdf_preview_denial_commits_snapshot_scrubbing(django_user_model, monkeypatch, when):
    client, _, _, fact, job = _preview(django_user_model, "visit-pdf-invalid-" + when)
    if when == "before_render":
        revise_fact(job.patient, fact.pk, action="REVOKE", expected_revision=1)
    else:
        from apps.exports import views
        render = views.render_pdf

        def expire_while_rendering(snapshot):
            payload = render(snapshot)
            ExportJob.objects.filter(pk=job.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            return payload

        monkeypatch.setattr(views, "render_pdf", expire_while_rendering)
    result = client.get(f"/visit/{job.pk}/pdf/")
    assert result.status_code == 409
    job.refresh_from_db()
    assert job.status == ("INVALIDATED" if when == "before_render" else "EXPIRED")
    assert job.snapshot == {} and job.cleanup_pending


def test_multiple_treatment_dates_are_presented_as_unassociated_original_mentions(django_user_model):
    from apps.exports.pdf import card_sections, render_pdf
    from tests.exports.test_formats import _pdf

    _, patient = _patient(django_user_model, "visit-treatment-dates")
    body = "治疗经过：2025年记录原方案；2026年8月予以合成方案，具体日未记载。"
    _, version = parsed_facts(patient, [body])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "details": True})
    section = next(section for section in card_sections(snapshot) if section["key"] == "treatment")
    assert "原文提及多个日期" in section["entries"][0]["text"]
    assert "未逐项关联治疗" in section["entries"][0]["text"]
    document = _pdf(render_pdf(snapshot), "card-multiple-treatment-dates.pdf")
    rendered = "".join(page.extract_text() for page in document.pages).replace("\n", "")
    assert body in rendered and "未逐项关联治疗" in rendered
