from django.core.management.base import BaseCommand

from apps.operations.permissions import provision_role_groups


class Command(BaseCommand):
    help = "Create the fixed least-privilege operations role groups."

    def handle(self, *args, **options):
        groups = provision_role_groups()
        self.stdout.write(self.style.SUCCESS(f"Provisioned {len(groups)} operations roles."))
