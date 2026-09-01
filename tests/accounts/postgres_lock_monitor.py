from dataclasses import dataclass
import time


@dataclass(frozen=True)
class PostgresLockObservation:
    wait_event_type: str | None
    blocking_pids: tuple[int, ...]


def wait_until_backend_is_blocked_by(
    fetch_state,
    *,
    request_pid,
    blocker_pid,
    timeout_seconds=10.0,
    poll_interval_seconds=0.05,
    monotonic=time.monotonic,
    pause=time.sleep,
):
    """Return only after PostgreSQL reports the exact lock-wait relationship.

    The polling pause limits monitor-query frequency; elapsed time alone never
    satisfies the condition.
    """
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("timeout and poll interval must be positive")
    deadline = monotonic() + timeout_seconds
    observation = PostgresLockObservation(None, ())
    while True:
        row = fetch_state(request_pid)
        if row is None:
            observation = PostgresLockObservation(None, ())
        else:
            wait_event_type, blocking_pids = row
            observation = PostgresLockObservation(
                wait_event_type,
                tuple(blocking_pids or ()),
            )
        if (
            observation.wait_event_type == "Lock"
            and blocker_pid in observation.blocking_pids
        ):
            return observation

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"request backend {request_pid} was not observed waiting on a lock "
                f"held by backend {blocker_pid} before {timeout_seconds:.3f}s; "
                f"last observation was {observation!r}"
            )
        pause(min(poll_interval_seconds, remaining))
