from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import AccountSession


def register_account_session(account_id, session_key):
    if not account_id or not session_key:
        return None
    session, _created = AccountSession.objects.get_or_create(
        session_key=session_key,
        defaults={"account_id": account_id},
    )
    if session.account_id != account_id:
        session.account_id = account_id
        session.save(update_fields=["account", "last_seen_at"])
    return session


def unregister_account_session(session_key):
    if session_key:
        AccountSession.objects.filter(session_key=session_key).delete()


def revoke_account_sessions(account_id):
    keys = set(
        AccountSession.objects.filter(account_id=account_id).values_list("session_key", flat=True)
    )
    target = str(account_id)
    # Legacy sessions created before the registry deployment are decoded once
    # during deletion so revocation is complete across a rolling upgrade.
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if session.session_key in keys:
            continue
        try:
            session_account_id = session.get_decoded().get("_auth_user_id")
        except Exception:
            continue
        if session_account_id == target:
            keys.add(session.session_key)
    if keys:
        Session.objects.filter(session_key__in=keys).delete()
        AccountSession.objects.filter(session_key__in=keys).delete()
    return len(keys)
