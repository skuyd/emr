from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.crypto import InvalidCiphertext, decrypt_phone, encrypt_phone, hash_phone
from apps.accounts.models import Account
from apps.accounts.phone import normalize_mainland_phone


DEVELOPMENT_PHONE = "18000000000"
DEVELOPMENT_PASSWORD = "123321"


def _matches_phone(ciphertext, phone):
    try:
        return decrypt_phone(ciphertext) == phone
    except InvalidCiphertext:
        return False


class Command(BaseCommand):
    help = "Create or refresh the local development account."

    def handle(self, *args, **options):
        if settings.DEBUG is not True or getattr(settings, "OTP_PROVIDER", None) != "development":
            raise CommandError("This command is unavailable.")

        phone = normalize_mainland_phone(DEVELOPMENT_PHONE)
        phone_hash = hash_phone(phone)
        encrypted_phone = encrypt_phone(phone)

        with transaction.atomic():
            account, _ = Account.objects.get_or_create(
                phone_hash=phone_hash,
                defaults={"phone_encrypted": encrypted_phone},
            )
            account = Account.objects.select_for_update().get(pk=account.pk)
            updates = []
            if not _matches_phone(account.phone_encrypted, phone):
                account.phone_encrypted = encrypted_phone
                updates.append("phone_encrypted")
            account.set_password(DEVELOPMENT_PASSWORD)
            updates.append("password")
            account.save(update_fields=updates)

        self.stdout.write("Development account is ready.")
