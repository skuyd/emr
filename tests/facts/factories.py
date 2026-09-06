from decimal import Decimal
import uuid

from django.utils import timezone

from apps.documents.models import ProcessingRun, ProcessingStage
from apps.processing.models import DocumentSummary, OcrBlock, ParsingVersion, ParsingVersionStatus
from tests.documents.test_detail_viewer import _document


def parsed_facts(patient, texts, *, document=None, document_type="DISCHARGE", polygons=True, previous=None):
    if document is None:
        document, pages = _document(patient, page_count=1)
    else:
        pages = list(document.pages.all())
    attempt = document.processing_runs.count() + 1
    run = ProcessingRun.objects.create(
        document=document, parser_version="facts-v1", task_type="synthetic", attempt_number=attempt,
        idempotency_key=str(uuid.uuid4()), stage=ProcessingStage.SUCCEEDED, finished_at=timezone.now(),
    )
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="facts-v1", ocr_provider="synthetic",
        ocr_provider_version="1", status=ParsingVersionStatus.READY, previous_version=previous,
    )
    DocumentSummary.objects.create(
        parsing_version=version, document_type=document_type, institution_raw="合成医院",
        document_date_raw="2026年8月", document_date="2026-08-01", date_precision="MONTH", confidence=Decimal("0.99"),
    )
    for index, text in enumerate(texts):
        OcrBlock.objects.create(
            parsing_version=version, document_page=pages[0], reading_order=index, text=text,
            polygon=[[.1, .1 + index * .02], [.8, .1 + index * .02], [.8, .12 + index * .02], [.1, .12 + index * .02]] if polygons else [],
            confidence=Decimal(".98"),
        )
    from apps.facts.extraction import extract_version_facts

    extract_version_facts(version)
    version = ParsingVersion.objects.activate(version)
    return document, version
