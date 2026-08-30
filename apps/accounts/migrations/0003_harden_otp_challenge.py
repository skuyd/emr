from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_otpchallenge"),
    ]

    operations = [
        migrations.AlterField(
            model_name="otpchallenge",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("ready", "Ready"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                ],
                default="ready",
                max_length=8,
            ),
        ),
    ]
