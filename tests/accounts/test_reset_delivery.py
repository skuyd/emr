from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import get_hasher
from django.test import Client
from django.test import override_settings
from django.utils import timezone

from apps.accounts.crypto import hash_phone
from apps.accounts.models import OtpChallenge
from tests.accounts.fakes import RecordingSmsProvider, FailingSmsProvider, CrashingAfterAcceptingSmsProvider, SimulatedProcessDeath


@pytest.fixture
def account(django_user_model):
    return django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"), phone_encrypted="synthetic", password="Original synthetic passphrase"
    )


@pytest.mark.django_db
def test_real_and_decoy_reset_requests_never_send_sms_in_http(client, account, monkeypatch):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    assert client.post("/login/forgot-password/", {"phone": "13800138000"}).status_code == 302
    assert Client().post("/login/forgot-password/", {"phone": "13900000000"}).status_code == 302
    assert provider.codes == []
    from apps.accounts.models import SmsDeliveryJob
    assert SmsDeliveryJob.objects.count() == 2
    assert SmsDeliveryJob.objects.filter(challenge__isnull=True).count() == 1


@pytest.mark.django_db
def test_reset_outbox_recovers_failed_and_lost_workers_until_expiration(client, account):
    from apps.accounts.models import SmsDeliveryJob
    from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries

    client.post("/login/forgot-password/", {"phone": "13800138000"})
    job = SmsDeliveryJob.objects.get()
    assert "13800138000" not in job.payload_encrypted
    challenge = OtpChallenge.objects.get()
    assert challenge.delivery_status == OtpChallenge.DeliveryStatus.PENDING
    now = timezone.now()
    assert deliver_sms_job(job.pk, provider=FailingSmsProvider(), now=now) == "retry"
    assert SmsDeliveryJob.objects.filter(pk=job.pk).exists()
    assert job.pk not in due_sms_deliveries(now=now)
    crashed = CrashingAfterAcceptingSmsProvider()
    with pytest.raises(SimulatedProcessDeath):
        deliver_sms_job(job.pk, provider=crashed, now=now + timedelta(seconds=31))
    assert job.pk not in due_sms_deliveries(now=now + timedelta(seconds=32))
    assert job.pk in due_sms_deliveries(now=now + timedelta(seconds=92))
    provider = RecordingSmsProvider()
    assert deliver_sms_job(job.pk, provider=provider, now=now + timedelta(seconds=92)) == "sent"
    assert provider.codes == crashed.codes
    assert not SmsDeliveryJob.objects.filter(pk=job.pk).exists()
    assert deliver_sms_job(job.pk, provider=provider) == "missing"
    assert len(provider.codes) == 1


@pytest.mark.django_db
def test_expired_or_revoked_outbox_erases_payload_without_sending(client, account):
    from apps.accounts.models import SmsDeliveryJob
    from apps.accounts.sms_delivery import deliver_sms_job

    client.post("/login/forgot-password/", {"phone": "13800138000"})
    job = SmsDeliveryJob.objects.get()
    provider = RecordingSmsProvider()
    assert deliver_sms_job(job.pk, provider=provider, now=timezone.now() + timedelta(minutes=6)) == "discarded"
    assert not SmsDeliveryJob.objects.exists()
    assert provider.codes == []


@pytest.mark.django_db
def test_real_and_decoy_wrong_reset_codes_do_equal_hash_work(account):
    from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries

    real, decoy = Client(), Client()
    real.post("/login/forgot-password/", {"phone": "13800138000"})
    decoy.post("/login/forgot-password/", {"phone": "13900000000"})
    provider = RecordingSmsProvider()
    for job_id in due_sms_deliveries():
        deliver_sms_job(job_id, provider=provider)
    wrong = "000000" if provider.last_code != "000000" else "000001"
    hasher = get_hasher()
    counts = []
    for test_client in (real, decoy):
        with patch.object(hasher, "encode", wraps=hasher.encode) as encoding:
            assert test_client.post("/login/forgot-password/verify/", {"code": wrong}).status_code == 400
            counts.append(encoding.call_count)
    assert counts == [1, 1]


@pytest.mark.django_db
def test_pending_delivery_verification_does_not_destroy_the_reset_flow(client, account):
    from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries

    client.post("/login/forgot-password/", {"phone": "13800138000"})
    assert client.post("/login/forgot-password/verify/", {"code": "000000"}).status_code == 400
    provider = RecordingSmsProvider()
    for job_id in due_sms_deliveries():
        deliver_sms_job(job_id, provider=provider)
    assert client.post("/login/forgot-password/verify/", {"code": provider.last_code}).status_code == 302


@pytest.mark.django_db
def test_real_and_decoy_reset_requests_perform_one_hash_each(client, account):
    hasher = get_hasher()
    for phone in ("13800138000", "13900000000"):
        with patch.object(hasher, "encode", wraps=hasher.encode) as encoding:
            assert client.post("/login/forgot-password/", {"phone": phone}).status_code == 302
            assert encoding.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize("phone", ["13800138000", "13900000000", "invalid"])
def test_every_reset_request_has_a_bounded_ip_outbox_and_neutral_response(client, account, phone):
    from apps.accounts.models import SmsDeliveryJob

    hasher = get_hasher()
    for _ in range(35):
        with patch.object(hasher, "encode", wraps=hasher.encode) as encoding:
            response = client.post("/login/forgot-password/", {"phone": phone})
            assert response.status_code == 302
            assert response.url == "/login/forgot-password/verify/"
            assert encoding.call_count == 1
    assert SmsDeliveryJob.objects.count() <= 30
    # The additional request boundary must retain the existing phone cooldown.
    assert OtpChallenge.objects.count() <= 1


@pytest.mark.django_db
def test_reset_request_ip_limit_is_shared_across_phone_values(client, account):
    from apps.accounts.models import SmsDeliveryJob

    for _ in range(30):
        client.post("/login/forgot-password/", {"phone": "invalid"})
    client.post("/login/forgot-password/", {"phone": "13800138000"})
    assert SmsDeliveryJob.objects.count() == 30
    assert not OtpChallenge.objects.exists()


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_NETWORKS=["172.20.0.10/32"])
def test_reset_limit_uses_trusted_client_and_does_not_accept_spoofed_forwarding(client):
    from apps.accounts.models import SmsDeliveryJob

    for _ in range(30):
        client.post("/login/forgot-password/", {"phone": "invalid"},
                    REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="203.0.113.10")
    client.post("/login/forgot-password/", {"phone": "invalid"},
                REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="203.0.113.11")
    assert SmsDeliveryJob.objects.count() == 30
    client.post("/login/forgot-password/", {"phone": "invalid"},
                REMOTE_ADDR="172.20.0.10", HTTP_X_FORWARDED_FOR="203.0.113.10")
    assert SmsDeliveryJob.objects.count() == 31


@pytest.mark.django_db
def test_reset_request_limit_reopens_after_its_hour_window():
    from apps.accounts.sms_delivery import allow_password_reset_request

    now = timezone.now()
    assert all(allow_password_reset_request("203.0.113.1", now=now) for _ in range(30))
    assert not allow_password_reset_request("203.0.113.1", now=now + timedelta(minutes=59))
    assert allow_password_reset_request("203.0.113.1", now=now + timedelta(hours=1))


@pytest.mark.django_db
def test_delivery_recovery_keeps_outbox_after_broker_failure(client, account, monkeypatch):
    from apps.accounts.models import SmsDeliveryJob
    from apps.accounts.tasks import deliver_sms, recover_sms_deliveries

    client.post("/login/forgot-password/", {"phone": "13800138000"})

    def unavailable(**_kwargs):
        raise ConnectionError("synthetic broker failure")

    monkeypatch.setattr(deliver_sms, "apply_async", unavailable)
    assert recover_sms_deliveries() == {"count": 1}
    assert SmsDeliveryJob.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("terminal", ["locked", "expired", "inactive", "pending", "consumed"])
def test_non_advancing_reset_verification_preserves_one_hash_cost(client, account, terminal):
    client.post("/login/forgot-password/", {"phone": "13800138000"})
    challenge = OtpChallenge.objects.get()
    if terminal == "locked":
        challenge.locked_at = timezone.now()
    elif terminal == "expired":
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
    elif terminal == "inactive":
        account.is_active = False
        account.save(update_fields=["is_active"])
    elif terminal == "consumed":
        challenge.consumed_at = timezone.now()
    challenge.save()
    hasher = get_hasher()
    with patch.object(hasher, "encode", wraps=hasher.encode) as encoding:
        assert client.post("/login/forgot-password/verify/", {"code": "000000"}).status_code == 400
        assert encoding.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["locked", "consumed", "inactive"])
def test_revocation_before_delivery_discards_sms_and_ciphertext(client, account, reason):
    from apps.accounts.models import SmsDeliveryJob
    from apps.accounts.sms_delivery import deliver_sms_job

    client.post("/login/forgot-password/", {"phone": "13800138000"})
    challenge = OtpChallenge.objects.get()
    if reason == "inactive":
        account.is_active = False
        account.save(update_fields=["is_active"])
    else:
        setattr(challenge, reason + "_at", timezone.now())
        challenge.save()
    job_id = SmsDeliveryJob.objects.get().pk
    provider = RecordingSmsProvider()
    assert deliver_sms_job(job_id, provider=provider) == "discarded"
    assert provider.codes == []
    assert not SmsDeliveryJob.objects.exists()
