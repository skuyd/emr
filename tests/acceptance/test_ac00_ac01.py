from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, ConsentRecord
from apps.accounts.session import IDLE_TIMEOUT_SECONDS
from apps.patients.models import Patient
from apps.patients.services import create_patient_space
from tests.accounts.fakes import RecordingSmsProvider


class _InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


@pytest.mark.django_db
@override_settings(OTP_FIXED_CODE="123456")
def test_ac00_ac01_http_otp_onboarding_session_return_and_one_patient(client, monkeypatch):
    """AC-00/AC-01 use the public HTTP flow; values below are synthetic test data."""
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    safe_next = "/records/?source=acceptance"

    requested = client.post("/login/request-code/", {"phone": "13900000000", "next": safe_next})
    assert requested.status_code == 200
    assert requested.context["request_accepted"] is True
    assert provider.last_code == "123456"

    verified = client.post(
        "/login/verify/",
        {"phone": "13900000000", "code": provider.last_code, "next": safe_next},
    )
    assert verified.status_code == 302
    assert verified["Location"] == "/onboarding/"

    onboarding = client.get("/onboarding/")
    assert onboarding.status_code == 200
    page = onboarding.content.decode()
    parser = _InputParser()
    parser.feed(page)
    business_inputs = [input_ for input_ in parser.inputs if input_.get("name") != "csrfmiddlewaretoken"]
    expected_fields = {"display_name", "privacy", "sensitive_data", "upload_authority"}
    assert {input_["name"] for input_ in business_inputs} == expected_fields
    assert {input_["name"] for input_ in business_inputs if "required" in input_} == expected_fields

    completed = client.post(
        "/onboarding/",
        {
            "display_name": "测试称呼",
            "privacy": "on",
            "sensitive_data": "on",
            "upload_authority": "on",
        },
    )
    assert completed.status_code == 302
    assert completed["Location"] == safe_next

    account = Account.objects.get()
    patient = Patient.objects.get(account=account)
    assert set(
        ConsentRecord.objects.filter(account=account, withdrawn_at__isnull=True).values_list("consent_type", flat=True)
    ) == {"privacy", "sensitive_data", "upload_authority"}
    assert create_patient_space(
        account,
        "different synthetic label",
        {},
        {"ip": "127.0.0.1", "user_agent": "acceptance-test"},
    ).pk == patient.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        Patient.objects.create(account=account, display_name="another synthetic label")

    session = client.session
    now = timezone.now()
    session["session_started_at"] = int(now.timestamp())
    session["session_last_seen_at"] = int((now - timedelta(seconds=IDLE_TIMEOUT_SECONDS)).timestamp())
    session.save()
    expired = client.get(safe_next)
    assert expired.status_code == 302
    assert expired["Location"] == "/login/?next=%2Frecords%2F%3Fsource%3Dacceptance"

    monkeypatch.setattr("apps.accounts.services._now", lambda: now + timedelta(seconds=61))
    requested_again = client.post("/login/request-code/", {"phone": "13900000000", "next": safe_next})
    assert requested_again.context["request_accepted"] is True
    returned = client.post(
        "/login/verify/",
        {"phone": "13900000000", "code": provider.last_code, "next": safe_next},
    )
    assert returned.status_code == 302
    assert returned["Location"] == safe_next
