import os
import subprocess
import sys

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings


def test_trial_requires_explicit_synthetic_data_acknowledgment():
    environment = {key: value for key, value in os.environ.items() if key != "SYNTHETIC_TRIAL_ACK"}
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.synthetic_trial"],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "SYNTHETIC-DATA-ONLY" in result.stderr


@override_settings(DEBUG=False, PRODUCTION_DEPLOYMENT=False, SYNTHETIC_TRIAL=True,
                   OTP_PROVIDER="synthetic_trial")
@pytest.mark.parametrize("path", ["/login/first-use/", "/login/first-use/password/", "/login/forgot-password/"])
def test_trial_does_not_enroll_or_reset_arbitrary_accounts(path):
    from apps.core.trial import SyntheticTrialMiddleware

    request = RequestFactory().post(path)
    response = SyntheticTrialMiddleware(lambda request: HttpResponse("app"))(request)
    assert response.status_code == 403


@override_settings(PRODUCTION_DEPLOYMENT=True, SYNTHETIC_TRIAL=True,
                   OTP_PROVIDER="synthetic_trial", DEBUG=False)
def test_trial_presentation_and_routes_do_not_override_production():
    from apps.core.trial import SyntheticTrialMiddleware, trial_environment

    request = RequestFactory().get("/login/first-use/")
    assert not trial_environment(request)["synthetic_trial"]
    assert SyntheticTrialMiddleware(lambda request: HttpResponse("app"))(request).status_code == 200
