from datetime import timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from freezegun import freeze_time

from apps.accounts.models import OtpChallenge
from apps.accounts.admin import OtpChallengeAdmin
from apps.accounts.services import DeliveryFailed, InvalidOtp, LockedOtp, ThrottledOtp, request_otp, verify_otp
from tests.accounts.fakes import (
    CrashingAfterAcceptingSmsProvider,
    FailingSmsProvider,
    LeakySmsProvider,
    RecordingSmsProvider,
    SimulatedProcessDeath,
)


@freeze_time("2026-08-30 08:00:00")
def test_otp_expires_at_exactly_five_minutes(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    code = provider.last_code

    with freeze_time(timezone.now() + timedelta(minutes=5)):
        with pytest.raises(LockedOtp):
            verify_otp("13800138000", code)


def test_fifth_wrong_attempt_locks_challenge(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    for _ in range(4):
        with pytest.raises(InvalidOtp):
            verify_otp("13800138000", "000000")
    with pytest.raises(LockedOtp):
        verify_otp("13800138000", "000000")


def test_successful_code_cannot_be_replayed(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)

    verify_otp("13800138000", provider.last_code)
    with pytest.raises(LockedOtp):
        verify_otp("13800138000", provider.last_code)


@freeze_time("2026-08-30 08:00:00")
def test_only_newest_challenge_can_be_verified(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    first_code = provider.last_code
    with freeze_time(timezone.now() + timedelta(seconds=60)):
        request_otp("13800138000", "203.0.113.1", provider)
        second_code = provider.last_code

        with pytest.raises(InvalidOtp):
            verify_otp("13800138000", first_code)
        assert verify_otp("13800138000", second_code).phone_hash


def test_provider_failure_creates_no_usable_challenge(db):
    with pytest.raises(DeliveryFailed):
        request_otp("13800138000", "203.0.113.1", FailingSmsProvider())

    challenge = OtpChallenge.objects.get()
    assert challenge.delivery_status == OtpChallenge.DeliveryStatus.FAILED
    assert challenge.otp_hash
    assert challenge.otp_hash != "123456"
    with pytest.raises(LockedOtp):
        verify_otp("13800138000", "123456")


def test_provider_failure_error_does_not_chain_raw_delivery_details(db):
    with pytest.raises(DeliveryFailed) as exc_info:
        request_otp("13800138000", "203.0.113.1", LeakySmsProvider())

    assert exc_info.value.__cause__ is None
    assert "13800138000" not in str(exc_info.value)


def test_otp_challenges_are_view_only_and_hide_sensitive_admin_fields():
    otp_admin = OtpChallengeAdmin(OtpChallenge, AdminSite())

    assert otp_admin.has_add_permission(None) is False
    assert otp_admin.has_change_permission(None) is False
    assert otp_admin.has_delete_permission(None) is False
    assert {"phone_hash", "phone_encrypted", "ip_hash", "otp_hash"} <= set(otp_admin.exclude)


def test_provider_process_death_after_acceptance_leaves_code_verifiable(db):
    provider = CrashingAfterAcceptingSmsProvider()

    with pytest.raises(SimulatedProcessDeath):
        request_otp("13800138000", "203.0.113.1", provider)

    assert verify_otp("13800138000", provider.last_code).pk


def test_cache_read_failure_falls_back_to_durable_limit(monkeypatch, db):
    monkeypatch.setattr(
        "apps.accounts.services.cache.get",
        lambda key: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    assert request_otp("13800138000", "203.0.113.1", RecordingSmsProvider()).pk


def test_cache_write_failure_does_not_fail_delivery(monkeypatch, db):
    monkeypatch.setattr(
        "apps.accounts.services.cache.set",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    assert request_otp("13800138000", "203.0.113.1", RecordingSmsProvider()).pk


@freeze_time("2026-08-30 08:00:00")
def test_phone_resend_limit_is_exactly_sixty_seconds(db):
    provider = RecordingSmsProvider()
    request_otp("13800138000", "203.0.113.1", provider)
    with freeze_time(timezone.now() + timedelta(seconds=59)):
        with pytest.raises(ThrottledOtp):
            request_otp("13800138000", "203.0.113.1", provider)
    with freeze_time(timezone.now() + timedelta(seconds=60)):
        request_otp("13800138000", "203.0.113.1", provider)


@freeze_time("2026-08-30 08:00:00")
def test_phone_limits_are_enforced_from_durable_challenges(db):
    provider = RecordingSmsProvider()
    base = timezone.now()
    for minute in range(5):
        with freeze_time(base + timedelta(minutes=minute)):
            request_otp("13800138000", "203.0.113.1", provider)
    with freeze_time(base + timedelta(minutes=5)):
        with pytest.raises(ThrottledOtp):
            request_otp("13800138000", "203.0.113.1", provider)

    for hour in range(1, 3):
        for minute in range(5):
            with freeze_time(base + timedelta(hours=hour, minutes=minute)):
                request_otp("13800138000", "203.0.113.1", provider)
    with freeze_time(base + timedelta(hours=3)):
        with pytest.raises(ThrottledOtp):
            request_otp("13800138000", "203.0.113.1", provider)


@freeze_time("2026-08-30 08:00:00")
def test_ip_hourly_limit_is_enforced_across_phone_hashes(db):
    provider = RecordingSmsProvider()
    base = timezone.now()
    for number in range(30):
        with freeze_time(base + timedelta(minutes=number)):
            request_otp(f"138{number:08d}", "203.0.113.1", provider)
    with freeze_time(base + timedelta(minutes=30)):
        with pytest.raises(ThrottledOtp):
            request_otp("13900000000", "203.0.113.1", provider)


@freeze_time("2026-08-30 08:00:00")
def test_throttle_uses_bounded_newest_and_database_count_queries(db):
    provider = RecordingSmsProvider()
    base = timezone.now()
    for days_before in range(22, 2, -1):
        with freeze_time(base - timedelta(days=days_before)):
            request_otp("13800138000", "203.0.113.1", provider)

    with CaptureQueriesContext(connection) as queries:
        request_otp("13800138000", "203.0.113.1", provider)

    challenge_sql = [query["sql"].upper() for query in queries if "OTPCHALLENGE" in query["sql"].upper()]
    assert any("COUNT(" in query for query in challenge_sql)
    assert any("LIMIT 1" in query for query in challenge_sql)
