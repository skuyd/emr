from django.db import migrations
from django.utils import timezone


def migrate_owners(apps, schema_editor):
    db = schema_editor.connection.alias
    Patient = apps.get_model("patients", "Patient")
    Membership = apps.get_model("patients", "PatientMembership")
    Preference = apps.get_model("patients", "PatientPreference")
    Deletion = apps.get_model("patients", "PatientDeletionJob")
    AccountDeletion = apps.get_model("accounts", "AccountDeletionJob")
    now = timezone.now()
    for patient in Patient.objects.using(db).select_related("account").iterator():
        # A disabled account may only be suspended. Only an explicit durable
        # deletion request can turn preserved patient data into deletion work.
        deleting = AccountDeletion.objects.using(db).filter(account_id=patient.account_id).exists()
        Membership.objects.using(db).get_or_create(patient_id=patient.pk, account_id=patient.account_id,
            defaults={"role": "ADMIN", "revoked_at": now if deleting else None})
        Preference.objects.using(db).filter(patient_id=patient.pk).update(account_id=patient.account_id)
        for app, name, field in (("documents", "Document", "created_by_id"),
                                  ("patients", "ProductFeedback", "created_by_id"),
                                  ("documents", "UploadBatch", "created_by_id"),
                                  ("exports", "ExportJob", "requested_by_id"),
                                  ("notifications", "PushSubscription", "account_id")):
            apps.get_model(app, name).objects.using(db).filter(patient_id=patient.pk).update(**{field: patient.account_id})
        if deleting:
            Patient.objects.using(db).filter(pk=patient.pk).update(deleted_at=now)
            Deletion.objects.using(db).get_or_create(patient_id=patient.pk, defaults={"requested_by_id": patient.account_id})
        apps.get_model("documents", "ProcessingRun").objects.using(db).filter(
            document__patient_id=patient.pk,
        ).update(requested_by_id=patient.account_id)
    Notification = apps.get_model("notifications", "TaskNotification")
    Receipt = apps.get_model("notifications", "NotificationReceipt")
    for notification in Notification.objects.using(db).select_related("patient").iterator():
        Receipt.objects.using(db).get_or_create(notification_id=notification.pk,
            account_id=notification.patient.account_id, defaults={"read_at": notification.read_at})


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0003_patientdeletionjob_patientmembership_and_more"),
        ("documents", "0007_processingrun_access_revision_and_more"),
        ("exports", "0002_exportjob_access_revision_exportjob_requested_by"),
        ("notifications", "0002_notificationreceipt_and_more"),
    ]
    operations = [migrations.RunPython(migrate_owners, migrations.RunPython.noop)]
