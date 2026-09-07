"""Invalidate artifacts through explicit glucose bindings."""


def invalidate_record_outputs(record):
    # Called under the Patient, source Document (when present), then Record locks.
    from apps.exports.models import ExportJob, ExportStatus
    from apps.exports.services import HIDDEN, _hide as hide_export
    from apps.patients.models import PatientShare
    from apps.patients.sharing import _hide as hide_share

    for job in ExportJob.objects.select_for_update().filter(glucose_sources__record=record).order_by('pk'):
        if job.status not in HIDDEN:
            hide_export(job, ExportStatus.INVALIDATED, '选定血糖记录已变化，请重新选择并生成。')
    for share in PatientShare.objects.select_for_update().filter(glucose_sources__record=record).order_by('pk'):
        if share.invalidated_at is None:
            hide_share(share, 'source_changed')
