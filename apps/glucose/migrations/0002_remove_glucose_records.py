from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def remove_glucose_outputs(apps, schema_editor):
    def contains_glucose(value):
        if isinstance(value, dict):
            return any(
                bool(item) if key in {
                    'glucose_record_ids', 'glucose_records', 'glucose_record_sources', 'glucose_document_ids',
                } else contains_glucose(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_glucose(item) for item in value)
        return False

    database = schema_editor.connection.alias
    jobs = apps.get_model('exports', 'ExportJob').objects.using(database)
    shares = apps.get_model('patients', 'PatientShare').objects.using(database)
    export_sources = apps.get_model('glucose', 'GlucoseExportSource').objects.using(database)
    share_sources = apps.get_model('glucose', 'GlucoseShareSource').objects.using(database)
    job_ids = [job.pk for job in jobs.iterator() if contains_glucose(job.snapshot) or contains_glucose(job.options)]
    jobs.filter(Q(pk__in=job_ids) | Q(pk__in=export_sources.values('job_id'))).update(
        snapshot={}, options={}, snapshot_digest='', status='INVALIDATED', filename='',
        cleanup_pending=True, cleanup_retry_at=None, lease_token=None, lease_expires_at=None,
    )
    share_ids = [share.pk for share in shares.iterator() if contains_glucose(share.snapshot) or contains_glucose(share.scope)]
    shares.filter(Q(pk__in=share_ids) | Q(pk__in=share_sources.values('share_id'))).update(
        snapshot={}, scope={}, snapshot_digest='', invalidated_at=timezone.now(), invalidation_reason='feature_removed',
    )


class Migration(migrations.Migration):
    dependencies = [('glucose', '0001_initial')]

    operations = [
        # Reversing recreates empty tables; deleted historical data is not restored.
        migrations.RunPython(remove_glucose_outputs, migrations.RunPython.noop),
        migrations.DeleteModel(name='GlucoseExportSource'),
        migrations.DeleteModel(name='GlucoseShareSource'),
        migrations.DeleteModel(name='GlucoseRevision'),
        migrations.DeleteModel(name='GlucoseRecord'),
    ]
