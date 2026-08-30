from .models import BatchStatus, UploadBatch


def task_navigation(request):
    patient = getattr(request, "patient", None)
    if patient is None:
        return {"navigation_active_task_count": 0}
    count = UploadBatch.objects.filter(patient=patient, status=BatchStatus.ACTIVE).count()
    return {"navigation_active_task_count": count}
