import pytest

from tests.accounts.postgres_lock_monitor import (
    PostgresLockObservation,
    wait_until_backend_is_blocked_by,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def pause(self, seconds):
        self.now += seconds


def test_lock_wait_monitor_requires_lock_wait_and_expected_blocker():
    request_pid = 101
    purge_pid = 202
    observations = iter(
        (
            ("Lock", [303]),
            ("Client", [purge_pid]),
            ("Lock", [purge_pid, 303]),
        )
    )
    clock = FakeClock()

    def fetch_state(pid):
        assert pid == request_pid
        return next(observations)

    observed = wait_until_backend_is_blocked_by(
        fetch_state,
        request_pid=request_pid,
        blocker_pid=purge_pid,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
        monotonic=clock.monotonic,
        pause=clock.pause,
    )

    assert observed == PostgresLockObservation("Lock", (purge_pid, 303))
    assert clock.now == pytest.approx(0.2)


def test_lock_wait_monitor_fails_at_bounded_deadline():
    clock = FakeClock()
    samples = 0

    def fetch_state(pid):
        nonlocal samples
        assert pid == 101
        samples += 1
        if samples > 4:
            raise RuntimeError("monitor exceeded the bounded sample count")
        return "Lock", [303]

    with pytest.raises(
        AssertionError,
        match="request backend 101 was not observed waiting on a lock held by backend 202",
    ):
        wait_until_backend_is_blocked_by(
            fetch_state,
            request_pid=101,
            blocker_pid=202,
            timeout_seconds=0.3,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            pause=clock.pause,
        )

    assert clock.now == pytest.approx(0.3)
    assert samples == 4
