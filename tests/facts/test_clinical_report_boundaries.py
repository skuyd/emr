import pytest

from apps.facts.clinical_readmodels import report_source_token
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from tests.facts.test_clinical_foundation import clinical_fixture


pytestmark = pytest.mark.django_db


def _spans(report):
    return [{"page_number": s.document_page.page_number, "ocr_block_id": str(s.ocr_block_id),
             "start_offset": s.start_offset, "end_offset": s.end_offset} for s in report.spans.all()]


@pytest.mark.parametrize("edit_replacement", [False, True])
def test_replacing_boundary_audits_fields_and_never_inherits_confirmation(django_user_model, edit_replacement):
    from apps.exports.content import build_snapshot, assert_snapshot_current
    from apps.exports.errors import SnapshotChanged
    from apps.facts.clinical_services import replace_report_boundary, revise_report

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="replace-" + str(edit_replacement))
    report = document.clinical_reports.get()
    for fact in report.fields.all():
        revise_fact(patient, fact.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(fact)["current_source_token"], checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all"})
    replacement = replace_report_boundary(patient, actor=patient.account, report_id=report.pk, spans=_spans(report),
                                         title="人工重划报告范围", expected_revision=0, expected_source=report_source_token(report))
    assert replacement.created_by_id == patient.account_id
    assert replacement.fields.count() == report.fields.count()
    assert all(not effective_fact(f)["usable"] for f in replacement.fields.all())
    assert report.revisions.get().action == "REPLACE"
    assert len(report.revisions.get().field_revisions) == report.fields.count()
    assert all(f.revisions.order_by("-sequence").first().action == "EXCLUDE" for f in report.fields.all())
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    report.refresh_from_db()
    if edit_replacement:
        field = replacement.fields.first()
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(field)["current_source_token"], checked_original=True)
        with pytest.raises(FactConflict):
            revise_report(patient, actor=patient.account, report_id=report.pk, action="UNDO", expected_revision=1,
                          expected_source=report_source_token(report))
        assert effective_fact(replacement.fields.get(pk=field.pk))["usable"]
    else:
        revise_report(patient, actor=patient.account, report_id=report.pk, action="UNDO", expected_revision=1,
                      expected_source=report_source_token(report))
        assert all(effective_fact(f)["usable"] for f in report.fields.all())
        assert all(effective_fact(f)["status"] == "EXCLUDED" for f in replacement.fields.all())
        assert len(replacement.revisions.get().field_revisions) == replacement.fields.count()


def test_page_and_raw_text_boundary_ui_rejects_ambiguous_anchor_and_stale_source(django_user_model):
    client, _, document, version, _ = clinical_fixture(django_user_model, name="boundary-ui")
    report = document.clinical_reports.get()
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    url = f"/facts/reports/{report.pk}/"
    form = {"action": "REPLACE", "expected_revision": 0, "expected_source": report_source_token(report),
            "title": "范围更正", "mode": "ocr", "first_page": 1, "last_page": 1,
            "first_ocr_block": str(blocks[2].pk), "last_ocr_block": str(blocks[3].pk),
            "start_text": "结节", "end_text": "建议结合临床。"}
    assert client.post(url, form).status_code == 400  # two identical starts in the first block
    form["start_text"] = "影像表现："
    form["expected_source"] = "0" * 64
    assert client.post(url, form).status_code == 409
    form["expected_source"] = report_source_token(report)
    response = client.post(url, form)
    assert response.status_code == 302
    replacement = document.clinical_reports.exclude(pk=report.pk).get()
    assert replacement.spans.count() == 2
    assert replacement.spans.first().raw_text.startswith("影像表现：")
    assert client.get(response.url).status_code == 200
