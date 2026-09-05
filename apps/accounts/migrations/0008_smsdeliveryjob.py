import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_authentication_flows")]

    operations = [migrations.CreateModel(
        name="SmsDeliveryJob",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("payload_encrypted", models.TextField()),
            ("expires_at", models.DateTimeField()),
            ("next_attempt_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ("lease_until", models.DateTimeField(null=True)),
            ("lease_token", models.UUIDField(null=True)),
            ("attempt_count", models.PositiveSmallIntegerField(default=0)),
            ("challenge", models.OneToOneField(null=True, on_delete=django.db.models.deletion.CASCADE,
                                             related_name="delivery_job", to="accounts.otpchallenge")),
        ],
    )]
