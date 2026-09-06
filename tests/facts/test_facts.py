import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_fact_review_entry_is_available_even_when_automatic_extraction_failed(django_user_model):
    client, patient = _patient(django_user_model, "facts-entry")
    document, _pages = _document(patient, status="PROCESSING_FAILED")
    response = client.get(f"/facts/documents/{document.pk}/")
    assert response.status_code == 200
    assert "人工补录" in response.content.decode()
    assert f"/records/{document.pk}/viewer/" in response.content.decode()


def test_explicit_sections_keep_negation_conflicts_and_partial_treatment_dates(django_user_model):
    from apps.facts.models import Fact
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-types")
    document, version = parsed_facts(patient, [
        "诊断：考虑肺部恶性肿瘤，尚不能确定。\n分期：临床分期未明确。",
        "治疗经过：2026年8月行手术，手术名称：肺叶切除术。",
        "影像结论：未见明确转移，建议结合病理。",
        "病理诊断：未发现恶性细胞。",
    ])
    rows = list(Fact.objects.filter(parsing_version=version).order_by("reading_order"))
    assert [row.category for row in rows] == ["DIAGNOSIS", "STAGE", "TREATMENT", "IMAGING", "PATHOLOGY"]
    assert rows[0].raw_text == "诊断：考虑肺部恶性肿瘤，尚不能确定。"
    assert rows[2].automatic_content["date"] == "2026-08"
    assert rows[2].automatic_content["date_precision"] == "MONTH"
    assert "未见明确转移" in rows[3].raw_text
    assert "未发现恶性细胞" in rows[4].raw_text
    assert all(row.document_id == document.pk and row.document_page.page_number == 1 and row.evidence_id for row in rows)
    other, other_version = parsed_facts(patient, ["诊断：明确恶性肿瘤。"])
    assert Fact.objects.filter(document__patient=patient, category="DIAGNOSIS").count() == 2
    assert other.pk != document.pk


def test_page_fallback_never_fabricates_region_and_multiline_context_is_complete(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-context")
    _, version = parsed_facts(patient, ["影像诊断：", "考虑炎症。", "未见明确占位。", "检查日期：2026-08-01"], document_type="IMAGING", polygons=False)
    fact = Fact.objects.get(parsing_version=version)
    assert fact.raw_text == "影像诊断：\n考虑炎症。\n未见明确占位。"
    assert fact.evidence.polygon is None
    assert effective_fact(fact)["source"]["location"] == "PAGE"


def test_confirmation_requires_original_check_and_exclude_revoke_and_undo_control_admission(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import usable_facts
    from apps.facts.revisions import revise_fact
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-admit")
    _, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    assert usable_facts(patient) == ()
    with pytest.raises(ValidationError):
        revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=False)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    assert len(usable_facts(patient)) == 1
    revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
    assert usable_facts(patient) == ()
    revise_fact(patient, fact.pk, action="UNDO", expected_revision=2)
    assert len(usable_facts(patient)) == 1
    revise_fact(patient, fact.pk, action="EXCLUDE", expected_revision=3)
    assert usable_facts(patient) == ()
    assert fact.revisions.count() == 4


def test_correction_survives_identical_reparse_but_changed_source_requires_new_confirmation(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import usable_facts, review_facts
    from apps.facts.revisions import revise_fact
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-reparse")
    document, first = parsed_facts(patient, ["诊断：考虑炎正。"])
    fact = Fact.objects.get(parsing_version=first)
    revise_fact(patient, fact.pk, action="CORRECT", changes={"text": "诊断：考虑炎症。"}, checked_original=True, expected_revision=0)
    _, second = parsed_facts(patient, ["诊断：考虑炎正。"], document=document, previous=first)
    rows = usable_facts(patient)
    assert len(rows) == 1 and rows[0]["content"]["text"] == "诊断：考虑炎症。"
    assert rows[0]["source"]["parsing_version"] == str(second.pk)
    _, third = parsed_facts(patient, ["诊断：未见炎症。"], document=document, previous=second)
    assert usable_facts(patient) == ()
    history = review_facts(patient, document=document, include_history=True)
    assert any(row["content"]["text"] == "诊断：考虑炎症。" and row["historical"] for row in history)
    fact.refresh_from_db()
    assert fact.raw_text == "诊断：考虑炎正。"
    assert fact.revisions.get().before["content"]["text"] == "诊断：考虑炎正。"


def test_manual_excerpt_has_source_audit_and_does_not_invent_dates(django_user_model):
    from apps.facts.readmodels import usable_facts
    from apps.facts.revisions import add_manual_fact, revise_fact

    _client, patient = _patient(django_user_model, "fact-manual")
    document, _pages = _document(patient, status="PROCESSING_FAILED")
    fact = add_manual_fact(patient, document.pk, page_number=1, category="TREATMENT", text="治疗经过：曾行手术，时间不详。")
    assert usable_facts(patient) == ()
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    row, = usable_facts(patient)
    assert row["origin"] == "MANUAL"
    assert row["content"]["date"] is None and row["content"]["date_precision"] == "UNKNOWN"
    assert row["source"]["parsing_version"] is None
    assert fact.created_by_id == patient.account_id and fact.created_at is not None


def test_stale_edit_cross_patient_and_unavailable_sources_cannot_confirm(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document
    from apps.facts.models import Fact
    from apps.facts.readmodels import usable_facts
    from apps.facts.revisions import FactConflict, revise_fact
    from tests.facts.factories import parsed_facts

    client, patient = _patient(django_user_model, "fact-owner")
    other_client, other = _patient(django_user_model, "fact-other")
    document, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    with pytest.raises(PermissionDenied):
        revise_fact(other, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    assert other_client.get(f"/facts/{fact.pk}/").status_code == 404
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    with pytest.raises(FactConflict):
        revise_fact(patient, fact.pk, action="REVOKE", expected_revision=0)
    move_to_trash(patient, document.pk)
    assert usable_facts(patient) == ()
    assert client.get(f"/facts/{fact.pk}/").status_code == 404
    with pytest.raises(PermissionDenied):
        revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=1, checked_original=True)
    restore_document(patient, document.pk)
    assert len(usable_facts(patient)) == 1


def test_changed_manual_page_requires_recheck_and_ambiguous_candidates_do_not_inherit_confirmation(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import usable_facts
    from apps.facts.revisions import add_manual_fact, revise_fact
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-ambiguity")
    document, first = parsed_facts(patient, ["诊断：考虑炎症。"])
    original = Fact.objects.get(parsing_version=first)
    manual = add_manual_fact(patient, document.pk, page_number=1, category="STAGE", text="分期：未明确。")
    for fact in (original, manual):
        revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    assert len(usable_facts(patient)) == 2
    _, second = parsed_facts(patient, ["诊断：考虑炎症。\n诊断：考虑炎症。"], document=document, previous=first)
    assert usable_facts(patient) == ()
    revise_fact(patient, manual.pk, action="CONFIRM", expected_revision=1, checked_original=True)
    row, = usable_facts(patient)
    assert row["id"] == str(manual.pk)
    assert row["source"]["parsing_version"] == str(second.pk)


def test_review_form_does_not_discard_edited_text_when_confirm_button_is_used(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact, usable_facts
    from tests.facts.factories import parsed_facts

    client, patient = _patient(django_user_model, "fact-form")
    _, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    row = effective_fact(fact)
    path = f"/facts/{fact.pk}/"
    assert client.get(path).status_code == 200
    values = {
        "category": row["category"], "institution": "合成医院", "record_date_raw": "2026年8月", "date_raw": "",
        "text": '诊断：未见肿物。 <script>alert("x")</script>',
        "expected_revision": 0, "expected_source": row["current_source_token"],
        "action": "CONFIRM", "checked_original": "on",
    }
    assert client.post(path, values).status_code == 400
    assert usable_facts(patient) == ()
    values["action"] = "CORRECT"
    assert client.post(path, values).status_code == 302
    assert "<script>alert" not in client.get(path).content.decode()
    assert "&lt;script&gt;" in client.get(path).content.decode()


def test_fact_and_revision_are_removed_by_document_permanent_cleanup(django_user_model):
    from apps.documents.deletion import request_document_deletion, purge_document_deletion
    from apps.facts.models import Fact, FactRevision
    from apps.facts.revisions import revise_fact
    from tests.documents.fakes import InMemoryObjectStore
    from tests.facts.factories import parsed_facts

    _client, patient = _patient(django_user_model, "fact-purge")
    document, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    job = request_document_deletion(patient, document.pk, dispatch=lambda job: None)
    purge_document_deletion(job.pk, InMemoryObjectStore())
    assert not Fact.objects.filter(pk=fact.pk).exists()
    assert not FactRevision.objects.filter(fact_id=fact.pk).exists()
