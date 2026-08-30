from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_harden_otp_challenge"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsentRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("consent_type", models.CharField(choices=[("privacy", "Privacy policy"), ("sensitive_data", "Sensitive information"), ("upload_authority", "Upload authority")], max_length=32)),
                ("policy_version", models.CharField(max_length=32)),
                ("policy_digest", models.CharField(max_length=64)),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("request_ip_hash", models.CharField(max_length=64)),
                ("user_agent_hash", models.CharField(max_length=64)),
                ("withdrawn_at", models.DateTimeField(blank=True, null=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="consent_records", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["account", "consent_type", "policy_version"], name="accounts_co_account_3f9d47_idx")],
                "constraints": [models.UniqueConstraint(condition=models.Q(("withdrawn_at__isnull", True)), fields=("account", "consent_type", "policy_version"), name="unique_active_consent_version")],
            },
        ),
    ]
