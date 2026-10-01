from datetime import date, time

from django.db import migrations, models


def backfill_local_date_time(apps, schema_editor):
    record_model = apps.get_model('self_records', 'DailyRecord')
    batch = []
    for record in record_model.objects.using(schema_editor.connection.alias).all().iterator(chunk_size=500):
        local = record.current_data['local_time']
        record.record_date = date.fromisoformat(local[:10])
        record.record_time = time.fromisoformat(local[11:16]) if len(local) > 10 else None
        batch.append(record)
        if len(batch) == 500:
            record_model.objects.using(schema_editor.connection.alias).bulk_update(batch, ['record_date', 'record_time'])
            batch.clear()
    if batch:
        record_model.objects.using(schema_editor.connection.alias).bulk_update(batch, ['record_date', 'record_time'])


class Migration(migrations.Migration):
    dependencies = [('self_records', '0002_dailyrecordexportsource_dailyrecordsharesource')]

    operations = [
        migrations.AddField('dailyrecord', 'record_date', models.DateField(null=True)),
        migrations.AddField('dailyrecord', 'record_time', models.TimeField(blank=True, null=True)),
        migrations.AlterField('dailyrecord', 'measured_at', models.DateTimeField(blank=True, null=True)),
        migrations.AlterField('dailyrecord', 'kind', models.CharField(
            max_length=16, choices=[('WEIGHT', '体重'), ('TEMPERATURE', '体温'), ('SYMPTOM', '症状'),
                                    ('ECOG', 'ECOG评分')])),
        migrations.RunPython(backfill_local_date_time, migrations.RunPython.noop),
        migrations.AlterField('dailyrecord', 'record_date', models.DateField()),
        migrations.RemoveIndex('dailyrecord', 'self_records_patient_time'),
        migrations.AddIndex('dailyrecord', models.Index(
            fields=['patient', 'deleted_at', '-record_date', '-record_time'], name='self_records_patient_day')),
        migrations.AlterModelOptions('dailyrecord', options={'ordering': ['-record_date', '-record_time', 'id']}),
    ]
