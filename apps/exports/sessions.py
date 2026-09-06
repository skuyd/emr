import hashlib
import hmac

from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.accounts.models import AccountSession
from apps.accounts.session import ABSOLUTE_TIMEOUT_SECONDS, IDLE_TIMEOUT_SECONDS


def session_digest(key):
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


def session_is_active(patient, digest, *, now=None):
    now = now or timezone.now()
    account = patient.account
    if not type(account).objects.filter(pk=account.pk, is_active=True).exists():
        return False
    keys = AccountSession.objects.filter(account=account).values_list("session_key", flat=True)
    key = next((key for key in keys if hmac.compare_digest(session_digest(key), digest)), None)
    session = Session.objects.filter(session_key=key, expire_date__gt=now).first() if key else None
    if session is None:
        return False
    data = session.get_decoded()
    started, seen = data.get("session_started_at"), data.get("session_last_seen_at")
    timestamp = int(now.timestamp())
    return (
        str(data.get("_auth_user_id")) == str(account.pk)
        and hmac.compare_digest(str(data.get("_auth_user_hash", "")), account.get_session_auth_hash())
        and type(started) is int and type(seen) is int
        and 0 <= timestamp - started < ABSOLUTE_TIMEOUT_SECONDS
        and 0 <= timestamp - seen < IDLE_TIMEOUT_SECONDS
    )
