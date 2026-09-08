from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings
import pytest

from apps.labs.review import create_review_task, transition_review_task
from apps.labs.revisions import effective_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_phase_two_workflows import case, _reviewer, _new_version

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("withdrawal", ["is_staff", "permission", "is_active"])
def test_review_post_rechecks_cached_actor_after_lock(case, django_user_model, monkeypatch, withdrawal):
    from apps.labs import review
    from tests.labs.test_phase_two_workflows import _withdraw_reviewer_authority

    _owner_client, patient, _document, row = case
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    transition_review_task(reviewer, task.pk, action="START", expected_revision=0)
    real_lock = review.lock_observation

    def revoke_after_lock(identity):
        locked = real_lock(identity)
        _withdraw_reviewer_authority(reviewer, withdrawal)
        return locked

    monkeypatch.setattr(review, "lock_observation", revoke_after_lock)
    response = client.post(f"/labs/reviews/{task.pk}/", {"action": "CONFIRM", "expected_revision": 1})
    assert response.status_code == 403
    row.refresh_from_db()
    task.refresh_from_db()
    assert row.revision_number == 0
    assert not row.revisions.exists()
    assert task.status == "IN_PROGRESS"
    assert task.revision_number == 1
    assert task.events.count() == 2


def test_reviewer_reference_uses_report_peers_without_exposing_them(case, django_user_model):
    from copy import copy
    import uuid
    from django.utils import timezone
    from apps.labs.dictionary import default_dictionary, release_digest, rules_digest
    from apps.operations.models import DictionaryRelease

    owner, patient, _, row = case
    rule = dict(id="scoped-sum", version="1", kind="report_sum", code="LAB_WBC", specimen="BLOOD",
                method="合成方法A", unit="10^9/L", component_codes=["LAB_NEUT_COUNT", "LAB_LYMPH_COUNT"],
                absolute_tolerance="0", reviewed_by="fixture", rationale="synthetic", evidence="fixture")
    baseline = default_dictionary()
    DictionaryRelease.objects.create(version=baseline.version, content_hash=baseline.content_hash,
        artifact_name=baseline.source_path.name, indicator_count=len(baseline.indicators), rules=[rule],
        rules_hash=rules_digest([rule]), release_hash=release_digest(baseline.content_hash, [rule]), published_at=timezone.now())
    row.raw_value, row.reference_range_raw = "5", "4-10"
    row.save(update_fields=["raw_value", "reference_range_raw"])
    peers = []
    for order, code, value in ((2, "LAB_NEUT_COUNT", "6"), (3, "LAB_LYMPH_COUNT", "1")):
        peer = copy(row)
        peer.pk, peer.reading_order, peer.standard_code, peer.raw_value = uuid.uuid4(), order, code, value
        peer.raw_name = f"PRIVATE_PEER_{order}"
        peer.save(force_insert=True)
        peers.append(peer)
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    response = client.get(f"/labs/reviews/{task.pk}/")
    owner_response = owner.get(f"/labs/observations/{row.pk}/")
    assert response.context["reference"] == owner_response.context["reference"]
    assert response.context["reference"]["label"] == "无法对照"
    assert "internal_conflict" in {item["code"] for item in response.context["issues"]}
    assert all(peer.raw_name not in response.content.decode() and str(peer.pk) not in response.content.decode() for peer in peers)
    assert all(client.get(f"/labs/observations/{peer.pk}/source/raw_value/").status_code == 404 for peer in peers)


def test_reviewer_history_rule_keeps_reason_without_disclosing_previous_report(case, django_user_model):
    from datetime import date
    from django.utils import timezone
    from apps.labs.dictionary import default_dictionary, release_digest, rules_digest
    from apps.operations.models import DictionaryRelease
    from tests.labs.test_phase_two_comparison import row as make_row

    owner, patient, _, row = case
    previous_document, previous = make_row(patient, day=date(2026, 7, 1), value="1.2345")
    previous.raw_name = "PRIVATE_PREVIOUS_REPORT"
    previous.save(update_fields=["raw_name"])
    rule = dict(id="scoped-history", version="1", kind="history_ratio", code="LAB_WBC", specimen="BLOOD",
                method="合成方法A", unit="10^9/L", minimum_ratio="2", reviewed_by="fixture", rationale="synthetic", evidence="fixture")
    baseline = default_dictionary()
    DictionaryRelease.objects.create(version=baseline.version, content_hash=baseline.content_hash,
        artifact_name=baseline.source_path.name, indicator_count=len(baseline.indicators), rules=[rule],
        rules_hash=rules_digest([rule]), release_hash=release_digest(baseline.content_hash, [rule]), published_at=timezone.now())
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    response = client.get(f"/labs/reviews/{task.pk}/")
    owner_response = owner.get(f"/labs/observations/{row.pk}/")
    assert "magnitude_suspect" in {item["code"] for item in owner_response.context["issues"]}
    assert "magnitude_suspect" in {item["code"] for item in response.context["issues"]}
    body = response.content.decode()
    assert all(value not in body for value in (previous.raw_name, previous.raw_value, "2026-07-01", str(previous.pk), str(previous_document.pk)))
    assert client.get(f"/labs/observations/{previous.pk}/source/raw_value/").status_code == 404


def test_review_correction_form_batches_fields_and_explicit_verified_reasons(case, django_user_model):
    import re
    from apps.labs.validation import validate_observation

    _, patient, _, row = case
    row.raw_value, row.raw_unit = "52", "unverified-unit"
    row.save(update_fields=["raw_value", "raw_unit"])
    row.evidence.confidence = "0.7000"
    row.evidence.save(update_fields=["confidence"])
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    url = f"/labs/reviews/{task.pk}/"
    assert client.post(url, {"action": "START", "expected_revision": 0}).status_code == 302
    body = client.get(url).content.decode()
    forms = re.findall(r"<form\b[^>]*>(.*?)</form>", body, re.S)
    corrections = [form for form in forms if 'value="CORRECT"' in form]
    assert len(corrections) == 1
    form = corrections[0]
    assert 'name="resolved_issues"' in form
    assert 'value="recognition_uncertain"' in form
    assert 'value="unit_unknown"' not in form
    assert all(f'name="{field}"' in form for field in ("raw_value", "raw_unit", "observation_date", "standard_code"))
    assert "提交更正并完成复核" in form
    data = {"action": "CORRECT", "expected_revision": 1, "correction_mode": "selected_fields",
            "change_fields": ["raw_value", "observation_date"], "raw_value": "5.2", "observation_date": "2026-08-21",
            "raw_name": "DO_NOT_APPLY", "raw_unit": "DO_NOT_APPLY", "standard_code": "LAB_PLT",
            "resolved_issues": ["recognition_uncertain"]}
    assert client.post(url, {**data, "resolved_issues": ["recognition_uncertain", "unit_unknown"]}).status_code == 400
    task.refresh_from_db()
    assert task.status == "IN_PROGRESS"
    assert client.post(url, data).status_code == 302
    row.refresh_from_db()
    task.refresh_from_db()
    effective = effective_observation(row)
    assert task.status == "COMPLETED"
    assert effective.raw_value == "5.2" and effective.observation_date.isoformat() == "2026-08-21"
    assert effective.raw_unit == "unverified-unit" and effective.standard_code == "LAB_WBC"
    issues = {item["code"] for item in validate_observation(effective)}
    assert "recognition_uncertain" not in issues and "unit_unknown" in issues


@pytest.mark.parametrize("revoke", ["account", "permission"])
def test_reviewer_source_refreshes_identity_and_permissions_after_render(case, django_user_model, monkeypatch, revoke):
    import io
    from tests.documents.test_detail_viewer import _pdf_bytes

    _, patient, _, row = case
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)

    class RevokingStore:
        def open_private(self, key):
            if revoke == "account":
                type(reviewer).objects.filter(pk=reviewer.pk).update(is_active=False)
            else:
                reviewer.user_permissions.clear()
            return io.BytesIO(_pdf_bytes(page_count=1))

    monkeypatch.setattr("apps.labs.views.get_object_store", RevokingStore)
    response = client.get(f"/labs/reviews/{task.pk}/source/raw_value/image/")
    assert response.status_code == 403
    assert not response.content.startswith(b"\x89PNG")


def test_mapping_correction_is_visible_in_result_and_before_after_history(case):
    from apps.labs.revisions import revise_observation

    client, patient, _, row = case
    revision = revise_observation(patient.account, row.pk, action="CORRECT", changes={"standard_code": "LAB_PLT"}, expected_revision=0)
    body = client.get(f"/labs/observations/{row.pk}/").content.decode()
    assert "当前标准项目" in body and "本次自动项目" in body
    assert revision.after["standard_name"] in body and "LAB_PLT" in body
    assert revision.before["standard_name"] in body and "LAB_WBC" in body
    assert body.count("LAB_PLT") >= 2 and body.count("LAB_WBC") >= 2


def test_optional_revision_ui_conflict_undo_and_owner_isolation(case, django_user_model):
    client, patient, document, row = case
    url = f"/labs/observations/{row.pk}/"
    response = client.get(url)
    assert response.status_code == 200
    assert all(label in response.content.decode() for label in ("与原件一致", "识别有误", "暂不处理", "撤销"))
    assert client.post(url, {"action": "REPORT_ERROR", "expected_revision": 0}).status_code == 302
    assert client.post(url, {"action": "CONFIRM", "expected_revision": 0}).status_code == 409
    assert client.post(url, {"action": "CORRECT", "expected_revision": 1, "raw_value": "6.2"}).status_code == 302
    row.refresh_from_db()
    assert effective_observation(row).raw_value == "6.2"
    assert client.post(url, {"action": "UNDO", "expected_revision": 2}).status_code == 302
    other_client, _ = _patient(django_user_model, "other-view")
    assert other_client.get(url).status_code in {403, 404}
    assert other_client.post(url, {"action": "CONFIRM", "expected_revision": 3}).status_code in {403, 404}
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(patient.account)
    assert csrf_client.post(url, {"action": "CONFIRM", "expected_revision": 3}).status_code == 403


def test_task_pages_and_sources_repeat_grant_version_checks(case, django_user_model):
    client, patient, document, row = case
    reviewer = _reviewer(django_user_model)
    staff_client = Client()
    staff_client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    url = f"/labs/reviews/{task.pk}/"
    assert staff_client.get(url).status_code == 200
    assert staff_client.get(f"/labs/observations/{row.pk}/").status_code in {403, 404}
    assert staff_client.get(f"/records/{document.pk}/").status_code != 200
    assert staff_client.post(url, {"action": "START", "expected_revision": 0}).status_code == 302
    transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=1)
    assert staff_client.get(url).status_code == 403
    assert staff_client.get(f"/labs/reviews/{task.pk}/source/raw_value/").status_code == 403
    assert staff_client.post(url, {"action": "CONFIRM", "expected_revision": 2}).status_code == 403


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


def test_source_images_are_real_private_pages_and_stop_after_reparse(case, django_user_model, monkeypatch):
    from tests.documents.fakes import InMemoryObjectStore
    from tests.documents.test_detail_viewer import _pdf_bytes

    client, patient, document, row = case
    reviewer = _reviewer(django_user_model)
    staff_client = Client()
    staff_client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = _pdf_bytes(page_count=1)
    monkeypatch.setattr("apps.labs.views.get_object_store", lambda: store)
    url = f"/labs/reviews/{task.pk}/source/raw_value/image/"
    image = staff_client.get(url)
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")
    assert image["Cache-Control"] == "private, no-store, max-age=0"
    _new_version(document, row)
    assert staff_client.get(url).status_code == 409
    assert staff_client.get(f"/labs/reviews/{task.pk}/").status_code == 409


def test_source_rechecks_revocation_during_rendering(case, django_user_model, monkeypatch):
    import io
    from tests.documents.test_detail_viewer import _pdf_bytes

    _, patient, document, row = case
    reviewer = _reviewer(django_user_model)
    staff_client = Client()
    staff_client.force_login(reviewer)
    task = create_review_task(patient.account, row.pk, reviewer=reviewer)

    class RevokingStore:
        def open_private(self, key):
            transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=0)
            return io.BytesIO(_pdf_bytes(page_count=1))

    monkeypatch.setattr("apps.labs.views.get_object_store", RevokingStore)
    response = staff_client.get(f"/labs/reviews/{task.pk}/source/raw_value/image/")
    assert response.status_code == 403
    assert not response.content.startswith(b"\x89PNG")


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
    response = client.get(f"/labs/observations/{latest.pk}/")
    assert len(response.context["history"]) == 1
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
    from django.contrib.auth.models import Permission
    manager.user_permissions.add(Permission.objects.get(codename="review_labobservation"))
    manager.phone_hash = hash_phone("+8613800138000")
    manager.set_password("valid-password")
    manager.save()
    manager_client = Client()
    manager_client.force_login(manager)
    _, patient = _patient(django_user_model, "dictionary-interface-owner")
    _, observation = make_row(patient, code="CANDIDATE_TEST", raw_name="合成白细胞别名")
    candidate = collect_dictionary_candidates(observation.parsing_version)[0]
    assert manager_client.get(f"/labs/dictionary/candidates/{candidate.pk}/").status_code == 403
    task = create_review_task(patient.account, observation.pk, reviewer=manager)
    assert manager_client.get("/labs/dictionary/").status_code == 200
    definition = next(item for item in json.loads(current_dictionary().source_path.read_text(encoding="utf-8"))["indicators"] if item["code"] == "LAB_WBC")
    observation.specimen = definition.get("specimen", "")
    observation.save(update_fields=["specimen"])
    definition["aliases"].append("合成白细胞别名")
    review_data = {"decision": "ACCEPT", "definition": json.dumps(definition), "rationale": "对照任务原件核实别名", "expected_revision": 0}
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
    transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=0)
    assert manager_client.get(review_url).status_code == 403


def test_review_owner_assignment_and_reviewer_correction_flow(case, django_user_model):
    client, _, _, row = case
    response = client.post(f"/labs/observations/{row.pk}/review/", {})
    assert response.status_code == 302
    task = row.review_tasks.get()
    reviewer = _reviewer(django_user_model)
    task_url = f"/labs/reviews/{task.pk}/"
    assert client.post(task_url, {"action": "ASSIGN", "reviewer": reviewer.pk, "expected_revision": 0}).status_code == 302
    staff_client = Client()
    staff_client.force_login(reviewer)
    assert staff_client.post(task_url, {"action": "START", "expected_revision": 1}).status_code == 302
    assert staff_client.post(task_url, {"action": "CORRECT", "expected_revision": 2, "raw_value": "6.2"}).status_code == 302
    row.refresh_from_db()
    effective = effective_observation(row)
    assert effective.raw_value == "6.2"
    assert effective.value_origin == "REVIEW"
    assert "已完成" in staff_client.get(task_url).content.decode()


def _actual_browser_optional_correction_comparison_and_review_grant(django_user_model, base_url, settings, monkeypatch):
    from pathlib import Path
    from urllib.parse import urlsplit
    from tests.browser.test_ac02_upload_browser import _browser_executable
    from tests.documents.fakes import InMemoryObjectStore
    from tests.documents.test_detail_viewer import _pdf_bytes
    from tests.labs.test_phase_two_comparison import row as make_row
    from playwright.sync_api import sync_playwright, expect

    executable = _browser_executable()
    if executable is None:
        pytest.skip("No local Chromium browser available")
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    client, patient = _patient(django_user_model, "actual-interface-browser")
    document, row = make_row(patient)
    row.evidence.confidence = "0.7000"
    row.evidence.save(update_fields=["confidence"])
    reviewer = _reviewer(django_user_model)
    reviewer_client = Client()
    reviewer_client.force_login(reviewer)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = _pdf_bytes(page_count=1)
    monkeypatch.setattr("apps.labs.views.get_object_store", lambda: store)
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    root = Path(__file__).resolve().parents[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                             "url": base_url}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def static(route):
            path = root / urlsplit(route.request.url).path.lstrip("/")
            route.fulfill(path=path) if path.is_file() else route.fulfill(status=404)

        page.route("**/static/**", static)
        page.goto(f"{base_url}/labs/observations/{row.pk}/", wait_until="networkidle")
        expect(page.get_by_role("heading", name="核对检验结果")).to_be_visible()
        expect(page.frame_locator("iframe").get_by_role("img", name="第 1 页原始报告")).to_have_js_property("naturalWidth", 600)
        page.get_by_text("主动更正（可选）", exact=True).click()
        page.get_by_label("结果正确内容", exact=True).fill("6.8")
        page.get_by_role("button", name="更正结果", exact=True).click()
        expect(page.locator(".labs-value")).to_contain_text("6.8")
        page.get_by_role("button", name="撤销上次操作", exact=True).click()
        expect(page.locator(".labs-value")).to_contain_text("5.2")
        page.get_by_text("授权内部复核（可选）", exact=True).click()
        page.get_by_label("复核人员账户编号（可留空，稍后分配）", exact=True).fill(str(reviewer.pk))
        page.get_by_role("button", name="创建复核任务", exact=True).click()
        expect(page.get_by_role("heading", name="资料转录复核")).to_be_visible()
        task_url = page.url
        reviewer_context = browser.new_context()
        reviewer_context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": reviewer_client.cookies[settings.SESSION_COOKIE_NAME].value, "url": base_url}])
        reviewer_page = reviewer_context.new_page()
        reviewer_page.route("**/static/**", static)
        reviewer_page.goto(task_url, wait_until="networkidle")
        reviewer_page.get_by_role("button", name="开始复核", exact=True).click()
        reviewer_page.get_by_text("主动更正（可选）", exact=True).click()
        reviewer_page.get_by_role("checkbox", name="更正结果", exact=True).check()
        reviewer_page.get_by_label("结果正确内容", exact=True).fill("6.9")
        reviewer_page.get_by_role("checkbox", name="更正日期", exact=True).check()
        reviewer_page.get_by_label("日期正确内容", exact=True).fill("2026-08-22")
        reviewer_page.locator('form:has(input[value="CORRECT"])').get_by_role("checkbox", name="已核实：识别不确定", exact=True).check()
        reviewer_page.get_by_role("button", name="提交更正并完成复核", exact=True).click()
        expect(reviewer_page.get_by_text("状态：已完成", exact=False)).to_be_visible()
        expect(reviewer_page.locator(".labs-value")).to_contain_text("6.9")
        expect(reviewer_page.get_by_text("2026-08-22", exact=True)).to_be_visible()
        page.reload(wait_until="networkidle")
        page.get_by_role("button", name="撤回授权", exact=True).click()
        expect(page.get_by_text("状态：已撤销", exact=False)).to_be_visible()
        reviewer_page.reload(wait_until="networkidle")
        expect(reviewer_page.get_by_text("无权访问，授权可能已撤回或需要重新验证身份。", exact=True)).to_be_visible()
        page.goto(f"{base_url}/labs/compare/", wait_until="networkidle")
        expect(page.get_by_role("table")).to_contain_text("6.9")
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert errors == []
        browser.close()


@override_settings(SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestActualBrowserReviewGrant(StaticLiveServerTestCase):
    from tests.browser.sqlite_server import SQLiteSerializedLiveServerThread

    # Only the shared in-memory SQLite connection needs serialized WSGI work.
    # PostgreSQL keeps concurrent requests and independent connections.
    server_thread_class = SQLiteSerializedLiveServerThread

    def test_actual_browser_optional_correction_comparison_and_review_grant(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model

        with pytest.MonkeyPatch.context() as monkeypatch:
            _actual_browser_optional_correction_comparison_and_review_grant(
                get_user_model(), self.live_server_url, settings, monkeypatch,
            )
