import pytest
from django.test import Client, override_settings

from apps.accounts.models import ConsentRecord
from apps.patients.models import Patient
from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "test"}


@pytest.mark.django_db
def test_onboarding_requires_authentication_and_csrf(client, django_user_model):
    assert client.get("/onboarding/").status_code == 302
    assert client.get("/onboarding/")["Location"] == "/login/?next=/onboarding/"

    account = django_user_model.objects.create(phone_hash="e" * 64, phone_encrypted="ciphertext")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(account)
    assert csrf_client.post("/onboarding/", {"display_name": "\u738b\u5c0f\u660e", **CONFIRMATIONS}).status_code == 403


@pytest.mark.django_db
def test_onboarding_page_has_only_required_fields_and_reachable_policy_links(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="f" * 64, phone_encrypted="ciphertext")
    client.force_login(account)

    response = client.get("/onboarding/")
    content = response.content.decode()
    assert response.status_code == 200
    assert 'name="display_name"' in content
    for field in CONFIRMATIONS:
        assert f'name="{field}"' in content
    assert 'placeholder="例如：妈妈、王女士、我自己"' in content
    assert 'href="/privacy/"' in content
    assert 'href="/onboarding/sensitive-information/"' in content
    for forbidden in ("sex", "age", "diagnosis", "phone", "medical_history", "身份证"):
        assert forbidden not in content
    assert client.get("/onboarding/sensitive-information/").status_code == 200


@pytest.mark.django_db
def test_initial_onboarding_uses_exact_caregiver_neutral_prd_copy(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="i" * 64, phone_encrypted="ciphertext")
    client.force_login(account)

    content = client.get("/onboarding/").content.decode()

    for copy in (
        "为谁整理资料？",
        "患者称呼",
        "例如：妈妈、王女士、我自己",
        "我确认有权上传并管理相关资料",
        "我已阅读并单独同意敏感个人信息处理规则",
        "开始整理",
    ):
        assert copy in content
    assert 'name="sensitive_data"' in content


@pytest.mark.django_db
def test_reconsent_shows_only_changed_policy_without_display_name_or_unchanged_confirmations(client, django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="j" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "\u738b\u5c0f\u660e", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["sensitive_data"] = {**policies["sensitive_data"], "version": "2026-09-01"}

    with override_settings(CONSENT_POLICIES=policies):
        response = client.get("/onboarding/")
        content = response.content.decode()
        assert "授权条款已更新" in content
        assert 'name="display_name"' not in content
        assert 'name="sensitive_data"' in content
        assert 'name="privacy"' not in content
        assert 'name="upload_authority"' not in content
        assert "2026-09-01" in content

        response = client.post("/onboarding/", {"sensitive_data": True})
        assert response["Location"] == "/"
        assert account.patient.display_name == "\u738b\u5c0f\u660e"
        assert ConsentRecord.objects.filter(account=account).count() == 4


@pytest.mark.django_db
def test_onboarding_post_creates_patient_and_redirects_to_deferred_safe_next(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="1" * 64, phone_encrypted="ciphertext")
    client.force_login(account)
    session = client.session
    session["post_onboarding_next"] = "/protected/?tab=1"
    session.save()

    response = client.post("/onboarding/", {"display_name": "\u738b\u5c0f\u660e", **CONFIRMATIONS})

    assert response.status_code == 302
    assert response["Location"] == "/protected/?tab=1"
    assert Patient.objects.filter(account=account).count() == 1
    assert ConsentRecord.objects.filter(account=account).count() == 3
    assert "post_onboarding_next" not in client.session


@pytest.mark.django_db
def test_root_redirects_incomplete_accounts_and_renders_home_for_current_consents(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="2" * 64, phone_encrypted="ciphertext")
    client.force_login(account)
    assert client.get("/")["Location"] == "/onboarding/"

    create_patient_space(account, "\u738b\u5c0f\u660e", CONFIRMATIONS, EVIDENCE)
    response = client.get("/")
    assert response.status_code == 200
    assert "家庭健康资料" in response.content.decode()


@pytest.mark.django_db
def test_authenticated_incomplete_account_is_sent_to_onboarding_from_login_page(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="9" * 64, phone_encrypted="ciphertext")
    client.force_login(account)

    response = client.get("/login/?next=/protected/")

    assert response["Location"] == "/onboarding/"


@pytest.mark.django_db
@override_settings(OTP_FIXED_CODE="123456")
def test_first_verified_login_defers_safe_next_until_onboarding(client, monkeypatch):
    from tests.accounts.fakes import RecordingSmsProvider

    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    client.post("/login/request-code/", {"phone": "13800138000", "next": "/protected/?tab=1"})
    response = client.post(
        "/login/verify/",
        {"phone": "13800138000", "code": provider.last_code, "next": "/protected/?tab=1"},
    )

    assert response["Location"] == "/onboarding/"
    assert client.session["post_onboarding_next"] == "/protected/?tab=1"


@pytest.mark.django_db
def test_changed_policy_version_forces_reconsent_without_duplicate_history(client, django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="3" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "\u738b\u5c0f\u660e", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["sensitive_data"] = {**policies["sensitive_data"], "version": "2026-09-01"}

    with override_settings(CONSENT_POLICIES=policies):
        assert client.get("/")["Location"] == "/onboarding/"
        response = client.post("/onboarding/", {"sensitive_data": True})
        assert response["Location"] == "/"
        assert ConsentRecord.objects.filter(account=account, consent_type="sensitive_data").count() == 2


@pytest.mark.django_db
def test_rendered_policy_content_matches_configured_sha256_digests(client, django_user_model, settings):
    import hashlib

    account = django_user_model.objects.create(phone_hash="k" * 64, phone_encrypted="ciphertext")
    client.force_login(account)
    onboarding = client.get("/onboarding/").content.decode()

    for policy_type, policy in settings.CONSENT_POLICIES.items():
        assert hashlib.sha256(policy["content"].encode("utf-8")).hexdigest() == policy["digest"]
        if policy_type == "privacy":
            content = client.get("/privacy/").content.decode()
        elif policy_type == "sensitive_data":
            content = client.get("/onboarding/sensitive-information/").content.decode()
        else:
            content = onboarding
        assert policy["content"] in content


def _conflicting_policies(settings):
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "a" * 64
    return policies


@pytest.mark.django_db
def test_policy_conflict_is_a_generic_accessible_503_for_authenticated_entrypoints(client, django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="l" * 64, phone_encrypted="ciphertext")
    client.force_login(account)

    with override_settings(CONSENT_POLICIES=_conflicting_policies(settings)):
        anonymous_client = Client()
        assert anonymous_client.get("/login/").status_code == 503
        for path in ("/", "/login/", "/onboarding/"):
            response = client.get(path)
            content = response.content.decode()
            assert response.status_code == 503
            assert "服务暂时不可用" in content
            assert "privacy" not in content
            assert "2026-08-30" not in content
            assert "a" * 64 not in content
        assert not Patient.objects.filter(account=account).exists()
        assert not ConsentRecord.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_policy_conflict_returns_generic_503_from_public_policy_pages(client, settings):
    with override_settings(CONSENT_POLICIES=_conflicting_policies(settings)):
        for path in ("/privacy/", "/onboarding/sensitive-information/"):
            response = client.get(path)
            assert response.status_code == 503
            assert "服务暂时不可用" in response.content.decode()


@pytest.mark.django_db
@override_settings(OTP_FIXED_CODE="123456")
def test_policy_conflict_after_otp_verification_never_creates_patient_or_consent(client, monkeypatch, settings):
    from tests.accounts.fakes import RecordingSmsProvider

    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    client.post("/login/request-code/", {"phone": "18600000000"})
    with override_settings(CONSENT_POLICIES=_conflicting_policies(settings)):
        response = client.post("/login/verify/", {"phone": "18600000000", "code": provider.last_code})

        assert response.status_code == 503
        assert "服务暂时不可用" in response.content.decode()
        assert not Patient.objects.exists()
        assert not ConsentRecord.objects.exists()
