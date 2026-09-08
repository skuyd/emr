import pytest

from apps.documents.archive import records_context
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.lesions.models import Lesion
from apps.lesions.readmodels import review_observations
from apps.lesions.services import create_lesion, rename_lesion, undo_operation
from .test_relationships import matched_pair
from .test_views import family_pair


pytestmark = pytest.mark.django_db


def test_archive_finds_only_current_source_bound_names_without_multiplying_documents(django_user_model):
    patient, _, _ = matched_pair(django_user_model, "lesion-search-archive")
    lesion = Lesion.objects.get()
    rename = rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                           expected_revision=lesion.revision_number, name="随访检索标识紫杉")
    found = records_context(patient, {"q": "紫杉"})
    cards = [card for group in found["record_groups"] for card in group.cards]
    assert found["page_obj"].paginator.count == 2 and len({card.document.pk for card in cards}) == 2
    assert all(card.lesion_links[0]["id"] == str(lesion.pk) and "紫杉" in card.snippet for card in cards)
    assert records_context(patient, {"q": str(lesion.pk)})["page_obj"].paginator.count == 2
    assert records_context(patient, {"q": "观察 A"})["page_obj"].paginator.count == 0
    row = review_observations(patient, actor=patient.account)[0]
    field = Fact.objects.get(pk=next(field["id"] for field in row["fields"] if field["field_key"] == "lesion.site"))
    revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=field.revision_number,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    assert records_context(patient, {"q": "紫杉"})["page_obj"].paginator.count == 1
    undo_operation(patient, actor=patient.account, operation_id=rename.pk)
    assert records_context(patient, {"q": "紫杉"})["page_obj"].paginator.count == 0
    assert records_context(patient, {"q": "观察 A"})["page_obj"].paginator.count == 1


def test_lesion_search_uses_current_name_uuid_and_source_values_with_actual_patient_links(django_user_model):
    _, patient, client, actor, _ = family_pair(django_user_model, "lesion-search-http")
    rows = review_observations(patient, actor=actor)
    for index, row in enumerate(rows):
        create_lesion(patient, actor=actor, observation_id=row["id"], expected_revision=0,
                      expected_source=row["source_token"], checked_original=True, name=("蓝杉 <script>A</script>", "红枫")[index])
    target = Lesion.objects.get(original_name__startswith="蓝杉")
    page = client.get("/lesions/", {"patient": str(patient.pk), "q": "蓝杉"})
    assert page.status_code == 200 and len(page.context["lesions"]) == len(page.context["observations"]) == 1
    assert page.context["lesions"][0]["id"] == str(target.pk)
    assert "蓝杉 &lt;script&gt;A&lt;/script&gt;" in page.content.decode() and "<script>A</script>" not in page.content.decode()
    by_id = client.get("/lesions/", {"patient": str(patient.pk), "q": str(target.pk)})
    assert by_id.status_code == 200 and len(by_id.context["observations"]) == 1
    archive = client.get("/records/", {"patient": str(patient.pk), "q": "蓝杉"})
    assert archive.status_code == 200 and f'/lesions/{target.pk}/' in archive.content.decode()
    assert f'patient={patient.pk}' in archive.content.decode()
    measurement = client.get("/lesions/", {"patient": str(patient.pk), "q": "15mm"})
    assert measurement.status_code == 200 and len(measurement.context["observations"]) == 1
    assert measurement.context["observations"][0]["report_id"] == next(row["report_id"] for row in rows
        if any("15mm" in field["content"]["text"] for field in row["fields"]))
