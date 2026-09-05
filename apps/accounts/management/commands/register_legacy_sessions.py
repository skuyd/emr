from uuid import UUID

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.accounts.models import Account, AccountSession


class Command(BaseCommand):
    help = "Register valid pre-registry sessions once before upgrading session revocation."

    def handle(self, *args, **options):
        registered = 0
        for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=500):
            payload = session.get_decoded()
            try:
                account_id = UUID(payload.get("_auth_user_id", ""))
            except (ValueError, TypeError, AttributeError):
                continue
            with transaction.atomic():
                account = Account.objects.select_for_update().filter(pk=account_id, is_active=True).first()
                if account is None or not constant_time_compare(
                    payload.get("_auth_user_hash", ""), account.get_session_auth_hash()
                ):
                    continue
                if not Session.objects.filter(session_key=session.session_key, expire_date__gt=timezone.now()).exists():
                    continue
                _, created = AccountSession.objects.update_or_create(
                    session_key=session.session_key, defaults={"account_id": account_id}
                )
                registered += int(created)
        self.stdout.write(f"Registered {registered} legacy sessions.")
