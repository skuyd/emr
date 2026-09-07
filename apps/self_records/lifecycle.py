"""Source changes scrub only the derivatives that explicitly selected it."""


def invalidate_record_outputs(record):
    # Callers hold the Patient guard before this record and its derivatives.
    from apps.exports.models import ExportJob, ExportStatus
    from apps.exports.services import HIDDEN, _hide as hide_export
    from apps.patients.models import PatientShare
    from apps.patients.sharing import _hide as hide_share

    for job in ExportJob.objects.select_for_update().filter(self_record_sources__record=record).order_by('pk'):
        if job.status not in HIDDEN:
            hide_export(job, ExportStatus.INVALIDATED, '选定日常记录已变化，请重新选择并生成。')
    for share in PatientShare.objects.select_for_update().filter(self_record_sources__record=record).order_by('pk'):
        if share.invalidated_at is None:
            hide_share(share, 'source_changed')
