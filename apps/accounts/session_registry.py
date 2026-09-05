from django.contrib.sessions.models import Session

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
    if keys:
        Session.objects.filter(session_key__in=keys).delete()
        AccountSession.objects.filter(session_key__in=keys).delete()
    return len(keys)
