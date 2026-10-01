"""Remove dedicated treatment history; original documents and facts remain."""

from django.db import migrations
from django.utils import timezone


SELECTION_KEYS = ('treatment_event_ids', 'regimen_ids', 'cycle_ids', 'personal_change_ids')
ARRAY_KEYS = ('treatment_events', 'treatment_regimens', 'treatment_cycles', 'cycle_links',
              'cycle_points', 'cycle_key_nodes', 'personal_changes', 'derived_sources')


def invalidate_retired_outputs(apps, schema_editor):
    alias = schema_editor.connection.alias
    now = timezone.now()

    def contains_retired(snapshot, scope):
        return (any(snapshot.get(key) for key in ARRAY_KEYS)
                or any(scope.get(key) for key in SELECTION_KEYS)
                or any(snapshot.get('selection', {}).get(key) for key in SELECTION_KEYS)
                or any(snapshot.get('scope', {}).get(key) for key in SELECTION_KEYS)
                or snapshot.get('treatment_fingerprint')
                or any(snapshot.get('treatment_binding_ids', {}).values()))

    bound_jobs = set(apps.get_model('treatments', 'TreatmentExportSource').objects.using(alias)
                     .values_list('job_id', flat=True))
    bound_shares = set(apps.get_model('treatments', 'TreatmentShareSource').objects.using(alias)
                       .values_list('share_id', flat=True))
    jobs = apps.get_model('exports', 'ExportJob').objects.using(alias)
    for job in jobs.iterator():
        if job.pk in bound_jobs or contains_retired(job.snapshot, job.options):
            jobs.filter(pk=job.pk).update(
                status='INVALIDATED', snapshot={}, snapshot_digest='', options={}, filename='',
                cleanup_pending=True, cleanup_retry_at=None, lease_token=None, lease_expires_at=None,
                failures=[{'code': 'feature_removed', 'message': '治疗周期和个人变化功能已移除，请重新生成。'}],
            )
    shares = apps.get_model('patients', 'PatientShare').objects.using(alias)
    for share in shares.iterator():
        if share.pk in bound_shares or contains_retired(share.snapshot, share.scope):
            shares.filter(pk=share.pk).update(
                snapshot={}, snapshot_digest='', scope={}, invalidated_at=now,
                invalidation_reason='feature_removed',
            )


class Migration(migrations.Migration):
    dependencies = [
        ('treatments', '0005_treatmentexportsource_treatmentsharesource'),
    ]

    operations = [
        # Reversing only recreates empty tables; deleted historical data is not restored.
        migrations.RunPython(invalidate_retired_outputs, migrations.RunPython.noop),
        migrations.DeleteModel(name='CycleEventLink'),
        migrations.DeleteModel(name='CycleLineage'),
        migrations.DeleteModel(name='CycleRecordLink'),
        migrations.DeleteModel(name='TreatmentEvidence'),
        migrations.DeleteModel(name='TreatmentExportSource'),
        migrations.DeleteModel(name='TreatmentRevision'),
        migrations.DeleteModel(name='TreatmentShareSource'),
        migrations.DeleteModel(name='TreatmentCycle'),
        migrations.DeleteModel(name='TreatmentRegimen'),
        migrations.DeleteModel(name='TreatmentEvent'),
        migrations.DeleteModel(name='TreatmentDerivationRun'),
    ]
