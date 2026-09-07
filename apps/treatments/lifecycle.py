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
    events = set(TreatmentEvent.objects.using(using).filter(evidence__document_id=instance.pk).values_list("pk", flat=True))
    cycles = set(TreatmentCycle.objects.using(using).filter(
        Q(event_links__event_id__in=events) | Q(record_links__document_id=instance.pk)
        | Q(record_links__observation__parsing_version__document_id=instance.pk),
    ).values_list("pk", flat=True))
    regimens = set()
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
