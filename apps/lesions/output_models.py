"""Actual graph foreign keys invalidate output when a recorded source is lost."""
from django.core.exceptions import ValidationError
from django.db import models


class LesionOutputSource(models.Model):
    kind = models.CharField(max_length=12, choices=[('LESION', '病灶标识'), ('OBSERVATION', '报告观察')])
    original_id = models.UUIDField()
    lesion = models.ForeignKey('lesions.Lesion', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    observation = models.ForeignKey('lesions.LesionObservation', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        target = self.lesion if self.kind == 'LESION' else self.observation if self.kind == 'OBSERVATION' else None
        output = self.job if hasattr(self, 'job') else self.share
        if (target is None or self.original_id != target.pk or target.patient_id != output.patient_id
                or (self.lesion_id is not None and self.observation_id is not None)):
            raise ValidationError('输出来源须绑定同一患者的原始病灶标识或报告观察。')
        if self.kind == 'OBSERVATION':
            target.clean()


class LesionExportSource(LesionOutputSource):
    job = models.ForeignKey('exports.ExportJob', on_delete=models.CASCADE, related_name='lesion_sources')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'kind', 'original_id'], name='lesion_export_source_unique')]


class LesionShareSource(LesionOutputSource):
    share = models.ForeignKey('patients.PatientShare', on_delete=models.CASCADE, related_name='lesion_sources')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['share', 'kind', 'original_id'], name='lesion_share_source_unique')]
