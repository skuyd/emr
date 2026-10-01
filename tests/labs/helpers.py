from django.utils import timezone

from apps.documents.models import DocumentStatus, ProcessingRun, ProcessingStage
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.labs.quality import QUALITY_POLICY_VERSION
from apps.processing.models import (
    DatePrecision,
    DocumentMetadataCandidate,
    DocumentSummary,
    DocumentType,
    MetadataKind,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)
from tests.documents.test_detail_viewer import _document




def _observation(
    patient,
    observation_date,
    raw_value,
    *,
    code="LAB_WBC",
    standard_name="白细胞计数",
    raw_name="白细胞",
    raw_unit="10^9/L",
    method="合成方法A",
    institution="合成检验中心",
    result_type=ResultType.NUMERIC,
    precision=DatePrecision.DAY,
    capability=CapabilityLevel.STABLE,
    page_count=1,
    sampling_time='08:30',
):
    document, pages = _document(patient, page_count=page_count, status=DocumentStatus.ORGANIZED)
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
        stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(),
        is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=document,
        processing_run=run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        dictionary_version="1.0.0",
        dictionary_hash="a" * 64,
        status=ParsingVersionStatus.READY,
        diagnostics={"quality_policy": QUALITY_POLICY_VERSION},
    )
    ParsingVersion.objects.activate(version)
    DocumentSummary.objects.create(
        parsing_version=version,
        document_type=DocumentType.LAB,
        document_date_raw=observation_date.isoformat(),
        document_date=observation_date,
        date_precision=precision,
        institution_raw=institution,
        confidence="0.9000",
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[0],
        polygon=((0.1, 0.2), (0.6, 0.2), (0.6, 0.3), (0.1, 0.3)),
        source_text="synthetic evidence",
        confidence="0.9800",
    )
    date_evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[0],
        source_text="采样日期：" + observation_date.isoformat() + (' ' + sampling_time if sampling_time and precision == DatePrecision.DAY else ''),
        confidence="0.9800",
    )
    DocumentMetadataCandidate.objects.create(
        parsing_version=version,
        kind=MetadataKind.DOCUMENT_DATE,
        raw_text=date_evidence.source_text,
        normalized_value=observation_date.isoformat(),
        precision=precision,
        confidence="0.9800",
        evidence=date_evidence,
        selected=True,
    )
    if institution:
        institution_evidence = SourceEvidence.objects.create(
            parsing_version=version, document_page=pages[0], source_text=institution, confidence='0.9800',
        )
        DocumentMetadataCandidate.objects.create(
            parsing_version=version, kind=MetadataKind.INSTITUTION, raw_text=institution,
            normalized_value=institution, confidence='0.9800', evidence=institution_evidence, selected=True,
        )
    observation = LabObservation.objects.create(
        parsing_version=version,
        document_page=pages[0],
        evidence=evidence,
        reading_order=1,
        raw_name=raw_name,
        standard_code=code,
        standard_name=standard_name,
        raw_value=raw_value,
        result_type=result_type,
        raw_unit=raw_unit,
        observation_date=observation_date,
        institution_raw=institution,
        method_raw=method,
        capability_level=capability,
        dictionary_version="1.0.0",
        specimen="BLOOD",
    )
    return document, observation



def export_series(patient, code='LAB_WBC'):
    from apps.labs.readmodels import effective_rows
    from apps.labs.trends import _series_for_code

    rows = effective_rows(patient)
    return _series_for_code([(row, None) for row in rows if row.standard_code == code], previous=rows)
