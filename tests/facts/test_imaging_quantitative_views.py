from copy import deepcopy

import pytest

from apps.facts.readmodels import effective_fact
from tests.facts.test_imaging_quantitative import fields, imaging


pytestmark = pytest.mark.django_db


def scalar_post(patient, field, *, action="CONFIRM", **changes):
    row = effective_fact(field)
    value = row["content"]["value"]
    return {"patient_id": str(patient.pk), "action": action, "expected_revision": field.revision_number,
            "expected_source": row["current_source_token"], "raw_value": row["content"]["raw_value"],
            "scalar_1": value["values"][0], "scalar_2": value["values"][1] if len(value["values"]) == 2 else "",
            "comparator": value["comparator"], "original_unit": value["unit"] or "",
            "approximate": "on" if value["approximate"] else "", "measurement_role": value["measurement_role"],
            "checked_original": "on", **changes}


def test_scalar_http_actions_round_trip_values_sources_and_audit(django_user_model):
    client, patient, document, _, _ = imaging(django_user_model, "左肺上叶结节约12mm，SUVmax≤4.20。")
    suv = fields(document, "lesion.suvmax")[0]
    original = deepcopy(suv.automatic_content)
    url = f"/facts/{suv.pk}/"
    page = client.get(url)
    assert page.status_code == 200
    assert 'name="scalar_1"' in page.content.decode()
    assert "原单位未注明时留空" in page.content.decode()
    for action, state in [("CONFIRM", "CONFIRMED"), ("DEFER", "DEFERRED"), ("UNDO", "CONFIRMED"),
                          ("EXCLUDE", "EXCLUDED"), ("UNDO", "CONFIRMED"), ("REVOKE", "PENDING")]:
        response = client.post(url, scalar_post(patient, suv, action=action))
        assert response.status_code == 302, response.content.decode()
        suv.refresh_from_db()
        assert effective_fact(suv)["status"] == state
    response = client.post(url, scalar_post(patient, suv, action="CORRECT", scalar_1="3.7", comparator="EQ", raw_value="SUVmax3.7"))
    assert response.status_code == 302
    suv.refresh_from_db()
    assert effective_fact(suv)["content"]["value"]["values"] == ["3.7"]
    assert suv.automatic_content == original
    assert suv.revisions.count() == 7
    assert set(suv.revisions.values_list("author_id", flat=True)) == {patient.account_id}
    assert "SUVmax≤4.20" in client.get(url).content.decode()


def test_manual_reference_uses_comparison_entities_and_an_explicit_page(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token

    client, patient, document, _, _ = imaging(django_user_model, "对比前片（2025年）：左肺结节约12mm。")
    report = document.clinical_reports.get()
    url = f"/facts/reports/{report.pk}/"
    response = client.get(url, {"field_key": "comparison.reference_date"})
    assert response.status_code == 200
    form = response.context["form"]
    assert "对比" in form.fields["entity"].label
    choices = [key for key, _ in form.fields["entity"].choices]
    assert "comparison:001" in choices
    assert all(not key.startswith("lesion:") for key in choices)
    response = client.post(url, {"patient_id": str(patient.pk), "action": "manual_field",
        "field_key": "comparison.reference_date", "entity": "new", "page_number": 1,
        "expected_report_source": report_source_token(report), "raw_value": "对比日期不详", "date_value": "", "precision": "UNKNOWN"})
    assert response.status_code == 302, response.content.decode()
    manual = document.facts.get(origin="MANUAL", field_key="comparison.reference_date")
    assert manual.entity_key.startswith("comparison:")
    assert manual.created_by_id == patient.account_id
    assert manual.source_fragments.get().source_kind == "MANUAL"
    assert effective_fact(manual)["status"] == "PENDING"
    assert manual.automatic_content["value"] == {"value": None, "precision": "UNKNOWN"}


def test_maximum_choices_and_reference_warnings_are_visible_to_reviewer(django_user_model):
    client, patient, document, _, _ = imaging(django_user_model,
        "对比前片（2025年）：双肺见结节，较大者位于左肺，约12mm。")
    maximum = fields(document, "lesion.maximum_scope")[0]
    page = client.get(f"/facts/{maximum.pk}/")
    assert page.status_code == 200
    assert "本段较大者" in page.content.decode()
    assert "不能据此选为全报告最大病灶" in page.content.decode()
    form = page.context["form"]
    payload = {name: form[name].value() for name in form.fields}
    payload.update(patient_id=str(patient.pk), action="CONFIRM", checked_original="on")
    assert client.post(f"/facts/{maximum.pk}/", payload).status_code == 302
    reference = fields(document, "comparison.reference_date")[0]
    page = client.get(f"/facts/{reference.pk}/")
    assert "尚未关联到另一份检查" in page.content.decode()


def test_old_coded_side_can_be_confirmed_without_replacing_its_original_word_with_whole_clause(django_user_model):
    client, patient, document, _, _ = imaging(django_user_model, "左肺上叶结节约12mm。")
    side = fields(document, "lesion.laterality")[0]
    original = deepcopy(side.automatic_content)
    url = f"/facts/{side.pk}/"
    form = client.get(url).context["form"]
    payload = {name: form[name].value() for name in form.fields}
    payload.update(patient_id=str(patient.pk), action="CONFIRM", checked_original="on")
    assert client.post(url, payload).status_code == 302
    side.refresh_from_db()
    assert effective_fact(side)["content"] == original
    assert side.schema_version == "1.0" and effective_fact(side)["usable"]
