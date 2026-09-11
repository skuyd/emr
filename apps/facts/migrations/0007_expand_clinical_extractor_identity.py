from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("facts", "0006_merge_molecular_laterality")]

    operations = [
        migrations.AlterField(
            model_name="clinicalextraction",
            name="extractor_version",
            field=models.CharField(max_length=128),
        ),
    ]
