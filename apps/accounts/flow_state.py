from dataclasses import dataclass
from time import time
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from django.utils.http import url_has_allowed_host_and_scheme


SIGN_IN_PENDING_MFA_SESSION_KEY = "pending_mfa"
ENROLLMENT_PENDING_MFA_SESSION_KEY = "pending_enrollment_mfa"
PASSWORD_RESET_PENDING_MFA_SESSION_KEY = "pending_password_reset_mfa"
VERIFIED_PHONE_SESSION_KEY = "verified_phone"
_PENDING_MFA_MAX_AGE_SECONDS = 300
_PENDING_MFA_KEYS = {"account_id", "challenge_id", "destination", "issued_at"}
_PENDING_ENROLLMENT_KEYS = {"challenge_id", "destination", "issued_at"}
_VERIFIED_PHONE_KEYS = {"challenge_id", "verified_at"}


@dataclass(frozen=True)
class PendingMfaState:
    account_id: UUID
    challenge_id: int
    destination: str
    issued_at: int


@dataclass(frozen=True)
class PendingEnrollmentState:
    challenge_id: int
    destination: str
    issued_at: int


def safe_destination(request, value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    parsed = urlsplit(value)
    path = parsed.path
    for _ in range(3):
        decoded_path = unquote(path)
        if decoded_path == path:
            break
        path = decoded_path
    else:
        return ""
    if "%" in path or "\\" in path or any(ord(char) < 32 for char in path):
        return ""
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
        return ""
    if any(segment in {".", ".."} for segment in path.split("/")):
        return ""
    canonical = urlunsplit(("", "", path, parsed.query, ""))
    if path in {"/login", "/logout"} or path.startswith(("/login/", "/logout/")):
        return ""
    if not url_has_allowed_host_and_scheme(canonical, {request.get_host()}, request.is_secure()):
        return ""
    return canonical


def _issued_at():
    return int(time())


def store_pending_mfa(request, pending, destination):
    """Store sign-in state, using `/` only when the caller has no destination."""
    if destination in (None, ""):
        safe_destination_value = "/"
    else:
        safe_destination_value = safe_destination(request, destination)
        if not safe_destination_value:
            request.session.pop(SIGN_IN_PENDING_MFA_SESSION_KEY, None)
            raise ValueError("Invalid destination")
    request.session[SIGN_IN_PENDING_MFA_SESSION_KEY] = {
        "account_id": str(pending.account_id),
        "challenge_id": pending.challenge_id,
        "destination": safe_destination_value,
        "issued_at": _issued_at(),
    }


def load_pending_mfa(request):
    payload = request.session.get(SIGN_IN_PENDING_MFA_SESSION_KEY)
    state = _parse_pending_mfa(request, payload)
    if state is None:
        request.session.pop(SIGN_IN_PENDING_MFA_SESSION_KEY, None)
    return state


def store_pending_enrollment(request, challenge_id, destination):
    if destination in (None, ""):
        safe_destination_value = "/"
    else:
        safe_destination_value = safe_destination(request, destination)
        if not safe_destination_value:
            clear_enrollment_state(request)
            raise ValueError("Invalid destination")
    request.session[ENROLLMENT_PENDING_MFA_SESSION_KEY] = {
        "challenge_id": challenge_id,
        "destination": safe_destination_value,
        "issued_at": _issued_at(),
    }


def load_pending_enrollment(request):
    payload = request.session.get(ENROLLMENT_PENDING_MFA_SESSION_KEY)
    if not isinstance(payload, dict) or set(payload) != _PENDING_ENROLLMENT_KEYS:
        request.session.pop(ENROLLMENT_PENDING_MFA_SESSION_KEY, None)
        return None
    challenge_id = payload["challenge_id"]
    destination = payload["destination"]
    issued_at = payload["issued_at"]
    if (
        not isinstance(challenge_id, int)
        or isinstance(challenge_id, bool)
        or challenge_id <= 0
        or not isinstance(destination, str)
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not destination
        or safe_destination(request, destination) != destination
        or not _is_fresh(issued_at)
    ):
        request.session.pop(ENROLLMENT_PENDING_MFA_SESSION_KEY, None)
        return None
    return PendingEnrollmentState(challenge_id, destination, issued_at)


def store_verified_phone(request, challenge_id):
    request.session[VERIFIED_PHONE_SESSION_KEY] = {
        "challenge_id": challenge_id,
        "verified_at": _issued_at(),
    }


def load_verified_enrollment(request):
    pending = load_pending_enrollment(request)
    payload = request.session.get(VERIFIED_PHONE_SESSION_KEY)
    if not isinstance(payload, dict) or set(payload) != _VERIFIED_PHONE_KEYS:
        clear_enrollment_state(request)
        return None
    challenge_id = payload["challenge_id"]
    verified_at = payload["verified_at"]
    if (
        pending is None
        or not isinstance(challenge_id, int)
        or isinstance(challenge_id, bool)
        or challenge_id <= 0
        or not isinstance(verified_at, int)
        or isinstance(verified_at, bool)
        or challenge_id != pending.challenge_id
        or not _is_fresh(verified_at)
    ):
        clear_enrollment_state(request)
        return None
    return pending


def clear_enrollment_state(request):
    request.session.pop(ENROLLMENT_PENDING_MFA_SESSION_KEY, None)
    request.session.pop(VERIFIED_PHONE_SESSION_KEY, None)


def _is_fresh(timestamp):
    age = _issued_at() - timestamp
    return timestamp >= 0 and 0 <= age < _PENDING_MFA_MAX_AGE_SECONDS


def _parse_pending_mfa(request, payload):
    if not isinstance(payload, dict) or set(payload) != _PENDING_MFA_KEYS:
        return None
    account_id = payload["account_id"]
    challenge_id = payload["challenge_id"]
    destination = payload["destination"]
    issued_at = payload["issued_at"]
    if not isinstance(account_id, str) or not isinstance(challenge_id, int) or isinstance(challenge_id, bool):
        return None
    if challenge_id <= 0 or not isinstance(destination, str):
        return None
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        return None
    try:
        parsed_account_id = UUID(account_id)
    except (TypeError, ValueError, AttributeError):
        return None
    if not destination or safe_destination(request, destination) != destination:
        return None
    if not _is_fresh(issued_at):
        return None
    return PendingMfaState(parsed_account_id, challenge_id, destination, issued_at)
