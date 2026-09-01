import io
import logging

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.accounts.crypto import hash_phone
from apps.accounts.models import ConsentRecord
from apps.accounts.otp import generate_code
from apps.accounts.providers import DevelopmentSmsProvider, get_sms_provider
from apps.documents.models import Document, UploadBatch
from apps.patients.models import Patient


@override_settings(DEBUG=True, OTP_PROVIDER="development", OTP_FIXED_CODE="230412")
def test_development_provider_uses_spec_code():
    assert generate_code() == "230412"
    assert isinstance(get_sms_provider(), DevelopmentSmsProvider)


@override_settings(DEBUG=True, OTP_PROVIDER="console", OTP_FIXED_CODE="230412")
def test_fixed_code_is_not_used_outside_the_development_provider(monkeypatch):
    monkeypatch.setattr("apps.accounts.otp.secrets.randbelow", lambda maximum: 7)

    assert generate_code() == "000007"


@override_settings(DEBUG=True, OTP_PROVIDER="development", OTP_FIXED_CODE="not-a-code")
def test_malformed_development_fixed_code_fails_closed_without_echoing_configuration():
    with pytest.raises(ValueError) as raised:
        generate_code()

    assert "not-a-code" not in str(raised.value)


@pytest.mark.django_db
def test_seed_is_idempotent_preserves_owned_records_and_keeps_output_generic(settings, django_user_model):
    settings.DEBUG = True
    settings.OTP_PROVIDER = "development"
    output = io.StringIO()

    call_command("seed_development_account", stdout=output)
    account = django_user_model.objects.get(phone_hash=hash_phone("+8618000000000"))
    patient = Patient.objects.create(account=account, display_name="Existing patient")
    consent = ConsentRecord.objects.create(
        account=account,
        consent_type=ConsentRecord.ConsentType.PRIVACY,
        policy_version="test-version",
        policy_digest="a" * 64,
        request_ip_hash="b" * 64,
        user_agent_hash="c" * 64,
    )
    batch = UploadBatch.objects.create(patient=patient)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="preserved.png",
        content_type="image/png",
        byte_size=1,
        page_count=1,
        sha256="d" * 64,
        original_object_key="originals/preserved.png",
    )

    call_command("seed_development_account", stdout=output)

    account.refresh_from_db()
    assert django_user_model.objects.count() == 1
    assert account.pk == django_user_model.objects.get().pk
    assert Patient.objects.get().pk == patient.pk
    assert ConsentRecord.objects.get().pk == consent.pk
    assert Document.objects.get().pk == document.pk
    assert account.check_password("123321")
    assert output.getvalue() == "Development account is ready.\nDevelopment account is ready.\n"


@pytest.mark.django_db
def test_seed_refuses_without_exact_development_boundary(settings):
    settings.DEBUG = False
    settings.OTP_PROVIDER = "development"

    with pytest.raises(CommandError) as raised:
        call_command("seed_development_account")

    assert "development" not in str(raised.value).lower()


def test_development_provider_never_logs_otp_inputs(caplog):
    provider = DevelopmentSmsProvider()

    with caplog.at_level(logging.DEBUG):
        provider.send_otp("private-phone", "private-code", "private-purpose")

    assert "private-phone" not in caplog.text
    assert "private-code" not in caplog.text
    assert "private-purpose" not in caplog.text
