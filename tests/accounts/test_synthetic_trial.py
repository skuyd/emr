import pytest
from django.test import override_settings

from apps.accounts.otp import generate_code
from apps.accounts.providers import NullSmsProvider, get_sms_provider


@override_settings(DEBUG=False, PRODUCTION_DEPLOYMENT=False, SYNTHETIC_TRIAL=True,
                   OTP_PROVIDER="synthetic_trial", OTP_FIXED_CODE="837261")
def test_trial_auth_works_without_debug_pages_or_external_sms():
    assert generate_code() == "837261"
    assert isinstance(get_sms_provider(), NullSmsProvider)


@pytest.mark.parametrize("override", [
    {"PRODUCTION_DEPLOYMENT": True}, {"SYNTHETIC_TRIAL": False},
    {"SYNTHETIC_TRIAL": "True"}, {"DEBUG": True}, {"OTP_PROVIDER": "disabled"},
])
def test_trial_fixed_code_cannot_escape_its_exact_environment(override, monkeypatch):
    monkeypatch.setattr("apps.accounts.otp.secrets.randbelow", lambda maximum: 7)
    config = dict(DEBUG=False, PRODUCTION_DEPLOYMENT=False, SYNTHETIC_TRIAL=True,
                  OTP_PROVIDER="synthetic_trial", OTP_FIXED_CODE="837261")
    config.update(override)
    with override_settings(**config):
        assert generate_code() == "000007"
        with pytest.raises(RuntimeError, match="unavailable"):
            get_sms_provider().send_otp("synthetic", "000007", "sign_in")


@override_settings(DEBUG=False, PRODUCTION_DEPLOYMENT=False, SYNTHETIC_TRIAL=True,
                   OTP_PROVIDER="synthetic_trial", OTP_FIXED_CODE="invalid-private-value")
def test_malformed_trial_code_fails_without_disclosing_it():
    with pytest.raises(ValueError) as error:
        generate_code()
    assert "invalid-private-value" not in str(error.value)
