from django.test import Client
import pytest

from tests.documents.test_detail_viewer import _patient
from tests.labs.test_phase_two_workflows import case, _new_version

pytestmark = pytest.mark.django_db


def test_field_source_page_fallback_has_no_false_highlight(case):
    client, _, _, row = case
    row.field_evidence = {"raw_value": {"precision": "page", "page_number": 1, "polygon": None}}
    row.save(update_fields=["field_evidence"])
    response = client.get(f"/labs/observations/{row.pk}/source/raw_value/")
    assert response.status_code == 200
    assert response.context["highlight_rect"] == ""
    assert "页面定位" in response.content.decode()
    assert response["Cache-Control"] == "private, no-store, max-age=0"


def test_owner_can_switch_only_own_complete_parsing_version(case, django_user_model):
    client, _, document, row = case
    old = row.parsing_version
    new = _new_version(document, row)
    url = f"/labs/versions/{old.pk}/activate/"
    other_client, _ = _patient(django_user_model, "version-other")
    assert other_client.post(url, {"expected_active": new.parsing_version_id}).status_code in {403, 404}
    assert client.post(url, {"expected_active": old.pk}).status_code == 409
    assert client.post(url, {"expected_active": new.parsing_version_id}).status_code == 302
    old.refresh_from_db()
    assert old.active


def test_owner_source_images_are_real_private_pages_and_remain_historical_after_reparse(case, monkeypatch):
    from tests.documents.fakes import InMemoryObjectStore
    from tests.documents.test_detail_viewer import _pdf_bytes

    client, _patient, document, row = case
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = _pdf_bytes(page_count=1)
    monkeypatch.setattr("apps.labs.views.get_object_store", lambda: store)
    url = f"/labs/observations/{row.pk}/source/raw_value/image/"
    image = client.get(url)
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")
    assert image["Cache-Control"] == "private, no-store, max-age=0"
    _new_version(document, row)
    assert client.get(url).status_code == 200


def test_owner_source_stops_if_account_is_deactivated_during_render(case, monkeypatch):
    import io
    from tests.documents.test_detail_viewer import _pdf_bytes

    client, patient, _, row = case

    class DeactivatingStore:
        def open_private(self, key):
            type(patient.account).objects.filter(pk=patient.account_id).update(is_active=False)
            return io.BytesIO(_pdf_bytes(page_count=1))

    monkeypatch.setattr("apps.labs.views.get_object_store", DeactivatingStore)
    response = client.get(f"/labs/observations/{row.pk}/source/raw_value/image/")
    assert response.status_code in {403, 404}
    assert not response.content.startswith(b"\x89PNG")


def test_source_embed_and_carried_revision_history_are_available(case):
    from apps.labs.revisions import revise_observation

    client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    latest = _new_version(document, row)
    response = client.get(f"/labs/observations/{latest.pk}/", follow=True)
    assert len(response.context["row_items"][0]["history"]) == 1
    embed = client.get(f"/labs/observations/{latest.pk}/source/raw_value/?embed=1")
    assert "frame-ancestors 'self'" in embed["Content-Security-Policy"]
    assert "主要导航" not in embed.content.decode()
    assert embed.context["source_row"].pk == row.pk


def test_dictionary_end_to_end_requires_actual_second_factor_and_exact_sources(django_user_model, monkeypatch, settings):
    import json
    from uuid import uuid4
    from apps.accounts.crypto import hash_phone
    from apps.labs.dictionary import current_dictionary, default_dictionary
    from apps.labs.dictionary_workflow import collect_dictionary_candidates
    from apps.operations.permissions import Role
    from apps.patients.models import PatientMembership
    from tests.accounts.fakes import RecordingSmsProvider
    from tests.operations.test_services import staff
    from tests.labs.test_phase_two_comparison import row as make_row

    # The real OTP path sets a cooldown outside the rolled-back test database.
    # Keep that state private so later worker tests have their own SMS lifecycle.
    settings.CACHES = {"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": f"dictionary-review-{uuid4().hex}",
    }}
    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)
    manager.phone_hash = hash_phone("+8613800138000")
    manager.set_password("valid-password")
    manager.save()
    manager_client = Client()
    manager_client.force_login(manager)
    _, patient = _patient(django_user_model, "dictionary-interface-owner")
    _, observation = make_row(patient, code="CANDIDATE_TEST", raw_name="合成白细胞别名")
    candidate = collect_dictionary_candidates(observation.parsing_version)[0]
    assert manager_client.get(f"/labs/dictionary/candidates/{candidate.pk}/").status_code == 403
    PatientMembership.objects.create(patient=patient, account=manager, role='VIEWER')
    assert manager_client.get("/labs/dictionary/").status_code == 200
    definition = next(item for item in json.loads(current_dictionary().source_path.read_text(encoding="utf-8"))["indicators"] if item["code"] == "LAB_WBC")
    observation.specimen = definition.get("specimen", "")
    observation.save(update_fields=["specimen"])
    definition["aliases"].append("合成白细胞别名")
    review_data = {"decision": "ACCEPT", "definition": json.dumps(definition), "rationale": "对照患者原件核实别名", "expected_revision": 0}
    review_url = f"/labs/dictionary/candidates/{candidate.pk}/"
    assert manager_client.post(review_url, review_data).status_code == 403
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.providers.get_sms_provider", lambda: provider)
    verify_url = "/labs/dictionary/verify/"
    assert manager_client.post(verify_url, {"action": "SEND", "phone": "13800138000", "password": "wrong"}).status_code == 400
    assert provider.codes == []
    assert manager_client.post(verify_url, {"action": "SEND", "phone": "13800138000", "password": "valid-password"}).status_code == 200
    assert manager_client.post(verify_url, {"action": "VERIFY", "code": provider.last_code}).status_code == 302
    assert manager_client.post(review_url, review_data).status_code == 302
    publish_data = {"version": "interface-test-v2", "candidate_ids": [str(candidate.pk)]}
    response = manager_client.post("/labs/dictionary/preview/", publish_data)
    assert response.status_code == 200
    assert response.context["preview"]["regression_report"]["passed"]
    preview = response.context["preview"]
    publish_data.update(expected_active_hash=preview["expected_active_hash"], expected_preview_hash=preview["preview_hash"])
    assert manager_client.post("/labs/dictionary/publish/", publish_data).status_code == 302
    assert current_dictionary().match("合成白细胞别名").code == "LAB_WBC"
    from apps.operations.models import DictionaryRelease
    release = DictionaryRelease.objects.get(version="interface-test-v2")
    rollback_url = f"/labs/dictionary/releases/{release.previous_release_id}/rollback/"
    assert manager_client.post(rollback_url, {"expected_active_hash": current_dictionary().content_hash}).status_code == 302
    assert current_dictionary().match("合成白细胞别名") is None
    PatientMembership.objects.filter(patient=patient, account=manager).delete()
    assert manager_client.get(review_url).status_code == 403
