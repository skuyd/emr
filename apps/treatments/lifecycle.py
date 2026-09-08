"""Physical source deletion removes dependent medical baselines and decisions."""

from django.db.models import Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.documents.models import Document

from .models import CycleLineage, TreatmentCycle, TreatmentEvent, TreatmentRegimen


@receiver(pre_delete, sender=Document, dispatch_uid="treatments.purge_document_derivations")
def purge_document_derivations(sender, instance, using, **kwargs):
    # The document deletion service already holds Patient -> batches -> Document.
    # Keep this on the model signal too, so account/aggregate cascades cannot leave
    # copied medical content after only deleting Evidence foreign keys.
    invalidate_patient_outputs(instance.patient_id, using=using)
    events = set(TreatmentEvent.objects.using(using).filter(evidence__document_id=instance.pk).values_list("pk", flat=True))
    cycles = set(TreatmentCycle.objects.using(using).filter(
        Q(event_links__event_id__in=events) | Q(record_links__document_id=instance.pk)
        | Q(record_links__observation__parsing_version__document_id=instance.pk)
        | Q(record_links__report__document_id=instance.pk),
    ).values_list("pk", flat=True))
    regimens = set(TreatmentRegimen.objects.using(using).filter(events__in=events).values_list("pk", flat=True))
    cycles.update(TreatmentCycle.objects.using(using).filter(regimen_id__in=regimens).values_list("pk", flat=True))
    while cycles:
        before = len(cycles)
        related = CycleLineage.objects.using(using).filter(Q(predecessor_id__in=cycles) | Q(successor_id__in=cycles))
        for predecessor, successor in related.values_list("predecessor_id", "successor_id"):
            cycles.update((predecessor, successor))
        regimens.update(TreatmentCycle.objects.using(using).filter(pk__in=cycles).exclude(regimen_id=None).values_list("regimen_id", flat=True))
        cycles.update(TreatmentCycle.objects.using(using).filter(regimen_id__in=regimens).values_list("pk", flat=True))
        if len(cycles) == before:
            break
    # A group and its historical merge/split decisions can contain the removed
    # source's text. Remove that derived aggregate, retaining unrelated originals
    # and independent user event notes for a fresh source-based organization.
    TreatmentCycle.objects.using(using).filter(pk__in=cycles).delete()
    TreatmentRegimen.objects.using(using).filter(pk__in=regimens).delete()
    TreatmentEvent.objects.using(using).filter(pk__in=events).delete()


def invalidate_patient_outputs(patient_id, *, using="default"):
    # All treatment/changes outputs include full context in their fingerprint.
    # A new event or boundary can change their meaning even without a direct FK.
    from apps.exports.models import ExportJob, ExportStatus
    from apps.exports.services import HIDDEN, _hide as hide_export
    from apps.patients.models import PatientShare
    from apps.patients.sharing import _hide as hide_share
    from .models import TreatmentExportSource, TreatmentShareSource

    jobs = TreatmentExportSource.objects.using(using).values("job_id")
    for job in ExportJob.objects.using(using).select_for_update().filter(patient_id=patient_id, pk__in=jobs).exclude(status__in=HIDDEN).order_by("pk"):
        hide_export(job, ExportStatus.INVALIDATED, "治疗决定或组织边界已变化，请重新选择并生成。")
    shares = TreatmentShareSource.objects.using(using).values("share_id")
    for share in PatientShare.objects.using(using).select_for_update().filter(patient_id=patient_id, pk__in=shares, invalidated_at__isnull=True).order_by("pk"):
        hide_share(share, "source_changed")


@receiver(pre_delete, sender=TreatmentEvent, dispatch_uid="treatments.purge_event_outputs")
@receiver(pre_delete, sender=TreatmentRegimen, dispatch_uid="treatments.purge_regimen_outputs")
@receiver(pre_delete, sender=TreatmentCycle, dispatch_uid="treatments.purge_cycle_outputs")
def purge_treatment_outputs(sender, instance, using, **kwargs):
    invalidate_patient_outputs(instance.patient_id, using=using)
