import uuid

from django.utils import timezone
import pytest

from apps.documents.models import PatientUploadQuota
from apps.patients.models import PatientPreference, ProductFeedback
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_profile_shows_account_controls_privacy_and_current_quota_without_credentials(django_user_model):
    client, patient = _patient(django_user_model, "y")
    _document(patient, content_type="image/png", page_count=1)
    PatientUploadQuota.objects.create(
        patient=patient,
        document_limit=12,
        page_limit=80,
        storage_byte_limit=1024 * 1024,
    )

    response = client.get("/me/")
    content = response.content.decode()

    assert response.status_code == 200
    for expected in (
        "患者称呼",
        patient.display_name,
        "任务通知",
        "当前试用配额",
        "1 / 12 份",
        "1 / 80 页",
        "20 个文件、60 页",
        "查看隐私政策",
        "敏感个人信息处理规则",
        "提交产品意见",
        "删除全部资料并注销",
    ):
        assert expected in content
    assert "phone_hash" not in content and "phone_encrypted" not in content
    assert response["Cache-Control"] == "private, no-store, max-age=0"
    assert content.count('aria-current="page"') == 2
    assert 'aria-current="page">我的</a>' in content


def test_patient_name_update_reuses_onboarding_validation(django_user_model):
    client, patient = _patient(django_user_model, "z")

    invalid = client.post("/me/name/", {"display_name": "   "})
    valid = client.post("/me/name/", {"display_name": "  妈妈  "})

    assert invalid.status_code == 400
    patient.refresh_from_db()
    assert patient.display_name == "妈妈"
    assert valid.status_code == 302 and valid["Location"] == "/me/?name=saved#patient-name"
    assert "患者称呼已更新" in client.get(valid["Location"]).content.decode()
    assert client.get("/me/name/").status_code == 405


def test_product_feedback_is_scoped_minimal_and_rejects_control_characters(django_user_model):
    client, patient = _patient(django_user_model, "1")
    _other_client, other = _patient(django_user_model, "2")

    invalid = client.post("/me/feedback/", {"message": "bad\x00text"})
    valid = client.post("/me/feedback/", {"message": "  希望搜索按钮更醒目  "})

    assert invalid.status_code == 400
    assert valid.status_code == 302 and "feedback=thanks" in valid["Location"]
    feedback = ProductFeedback.objects.get()
    assert feedback.patient == patient
    assert feedback.message == "希望搜索按钮更醒目"
    assert not ProductFeedback.objects.filter(patient=other).exists()
    assert "产品意见已提交" in client.get(valid["Location"]).content.decode()
    assert client.get("/me/feedback/").status_code == 405


def test_notification_preference_requires_browser_grant_and_can_be_disabled(django_user_model):
    client, patient = _patient(django_user_model, "3")

    forged = client.post("/me/notifications/", {"enabled": "true", "prompted": "false", "permission": ""})
    preference = PatientPreference.objects.get(patient=patient)
    assert forged.status_code == 302
    assert preference.browser_notifications_enabled is False

    enabled = client.post(
        "/me/notifications/",
        {"enabled": "true", "prompted": "true", "permission": "granted"},
    )
    preference.refresh_from_db()
    assert enabled.status_code == 302 and "notification=on" in enabled["Location"]
    assert preference.browser_notifications_enabled is True
    assert preference.browser_notification_prompted_at is not None

    disabled = client.post(
        "/me/notifications/",
        {"enabled": "false", "prompted": "false", "permission": ""},
    )
    preference.refresh_from_db()
    assert disabled.status_code == 302 and "notification=off" in disabled["Location"]
    assert preference.browser_notifications_enabled is False
    assert client.get("/me/notifications/").status_code == 405


def test_notification_scripts_only_prompt_after_click_and_never_include_medical_content():
    profile_script = open("static/js/profile.js", encoding="utf-8").read()
    task_script = open("static/js/task-status.js", encoding="utf-8").read()
    station_script = open("static/js/notifications.js", encoding="utf-8").read()
    worker_script = open("static/service-worker.js", encoding="utf-8").read()

    assert 'form.addEventListener("submit"' in profile_script
    assert "Notification.requestPermission()" in profile_script
    assert profile_script.index('form.addEventListener("submit"') < profile_script.index("Notification.requestPermission()")
    assert "你上传的资料已整理完成，点击查看结果。" in worker_script
    for forbidden in ("指标名称", "raw_value", "诊断", "患者完整", "display_filename"):
        assert forbidden not in task_script + station_script + worker_script
