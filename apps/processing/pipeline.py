from contextlib import closing
from functools import lru_cache
import hashlib
import logging
import os
import threading

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from apps.documents.backends import get_object_store
from apps.documents.errors import ObjectNotFound, StorageTransportError, UploadDomainError
from apps.documents.models import Document, DocumentPage, ProcessingStage
from apps.labs.dictionary import current_dictionary, default_dictionary
from apps.labs.extraction import extract_observations
from apps.labs.models import LabObservation
from apps.labs.quality import QUALITY_POLICY_VERSION
from apps.labs.validation import VALIDATION_RULE_VERSION, validate_observation
from apps.facts.extraction import EXTRACTOR_VERSION, extract_version_facts
from apps.facts.models import ClinicalExtraction, ClinicalReport, Fact, FactExtraction

from .errors import NonRetryableProcessingError, RetryableProcessingError
from .geometry import source_polygon
from .material import classify_material
from .metadata import extract_document_metadata, observation_page_contexts
from .models import (
    DocumentMetadataCandidate,
    DocumentSummary,
    OcrBlock,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)
from .ocr.base import recognize_page
from .ocr.paddle import PaddleOcrProvider
from .ocr.text_layer import TextLayerOcrProvider
from .preparation import PreparedPageKind, prepare_document
from .runner import PipelineResult


def _combined_identity(values):
    values = tuple(sorted(set(values)))
    if len(values) == 1:
        return values[0]
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:12]
    return f"mixed-{digest}"


class DocumentProcessingPipeline:
    def __init__(self, *, object_store, raster_provider, dictionary=None, text_provider=None):
        self.object_store = object_store
        self.raster_provider = raster_provider
        self.dictionary = dictionary or default_dictionary()
        self.text_provider = text_provider

    def _text_provider(self):
        if self.text_provider is None:
            self.text_provider = TextLayerOcrProvider()
        return self.text_provider

    def _recognize(self, context, prepared):
        pages = []
        for page in prepared.pages:
            context.heartbeat(ProcessingStage.OCR)
            provider = self._text_provider() if page.kind == PreparedPageKind.TEXT_LAYER else self.raster_provider
            pages.append(recognize_page(provider, page))
        return tuple(pages)

    def _open_original(self, document):
        try:
            return self.object_store.open_private(document.original_object_key)
        except ObjectNotFound:
            raise NonRetryableProcessingError("original_unavailable") from None
        except StorageTransportError:
            raise RetryableProcessingError("storage_unavailable") from None
        except UploadDomainError:
            raise NonRetryableProcessingError("original_unavailable") from None

    def run(self, context):
        document = Document.objects.filter(pk=context.document_id, deleted_at__isnull=True).first()
        if document is None:
            raise NonRetryableProcessingError("document_unavailable")
        source = self._open_original(document)
        with closing(source), prepare_document(source, document.content_type) as prepared:
            pages = self._recognize(context, prepared)
            context.heartbeat(ProcessingStage.CLASSIFYING)
            observations = extract_observations(pages, self.dictionary)
            metadata = extract_document_metadata(pages, observation_count=len(observations))
            material = classify_material(prepared.pages, pages)
            context.heartbeat(ProcessingStage.EXTRACTING)
            self._persist(context, document, pages, observations, metadata, prepared.warnings, material=material)
            context.heartbeat(ProcessingStage.INDEXING)
        return PipelineResult.organized() if any(page.regions for page in pages) else PipelineResult.original_only()

    def _persist(self, context, document, pages, observations, metadata, warnings, *, material=None):
        page_contexts, metadata_candidates = observation_page_contexts(pages, observations, metadata)
        source_pages = {page.page_number: page for page in pages}

        def original_polygon(page_number, polygon):
            page = source_pages.get(page_number)
            return source_polygon(page.source_transform, polygon) if page else None

        def original_fields(fields):
            result = {}
            for name, field in fields.items():
                polygon = original_polygon(field.get("page_number"), field.get("polygon"))
                result[name] = {**field, "polygon": [list(point) for point in polygon] if polygon else None,
                                "precision": "region" if polygon and field.get("precision") == "region" else "page"}
            return result
        with transaction.atomic():
            context.assert_current()
            document_pages = {
                page.page_number: page
                for page in DocumentPage.objects.select_for_update().filter(document=document)
            }
            if set(document_pages) != {page.page_number for page in pages}:
                raise NonRetryableProcessingError("document_page_mismatch")
            version = ParsingVersion.objects.select_for_update().filter(processing_run_id=context.run_id).first()
            providers = [page.provider for page in pages]
            provider_versions = [page.provider_version for page in pages]
            values = {
                "document": document,
                "processing_run_id": context.run_id,
                "parser_version": context.parser_version,
                "ocr_provider": _combined_identity(providers),
                "ocr_provider_version": _combined_identity(provider_versions),
                "dictionary_version": self.dictionary.version,
                "dictionary_hash": self.dictionary.content_hash,
                # Capture the actual predecessor under the current document
                # lease before deriving source identities. Normal activation
                # preserves this link; inherited excerpt corrections/authors
                # must already participate in the READY collection receipt.
                "previous_version_id": ParsingVersion.objects.filter(document=document, active=True)
                    .values_list("pk", flat=True).first(),
                "status": ParsingVersionStatus.BUILDING,
                "active": False,
                "published_at": None,
            }
            if version is None:
                version = ParsingVersion.objects.create(**values)
            else:
                if version.active or version.status == ParsingVersionStatus.PUBLISHED or version.published_at is not None:
                    raise NonRetryableProcessingError("published_version_immutable")
                reports = ClinicalReport.objects.filter(parsing_version=version)
                facts = Fact.objects.filter(parsing_version=version)
                reviewed = ~Q(origin="AUTOMATIC") | Q(revision_number__gt=0) | Q(revisions__isnull=False)
                if reports.filter(reviewed).exists() or facts.filter(reviewed).exists():
                    raise NonRetryableProcessingError("reviewed_unpublished_version_immutable")
                # A retry rebuilds only this unreviewed, unpublished attempt.
                # Remove its report/span/field graph before restricted OCR FKs;
                # published history, manual work and revision audits stay intact.
                reports.delete()
                ClinicalExtraction.objects.filter(parsing_version=version).delete()
                LabObservation.objects.filter(parsing_version=version).delete()
                Fact.objects.filter(parsing_version=version, origin="AUTOMATIC").delete()
                FactExtraction.objects.filter(parsing_version=version).delete()
                DocumentSummary.objects.filter(parsing_version=version).delete()
                DocumentMetadataCandidate.objects.filter(parsing_version=version).delete()
                SourceEvidence.objects.filter(parsing_version=version).delete()
                OcrBlock.objects.filter(parsing_version=version).delete()
                for field, value in values.items():
                    if field not in {"document", "processing_run_id"}:
                        setattr(version, field, value)
                version.save()

            blocks = []
            for page in pages:
                document_page = document_pages[page.page_number]
                for region in page.regions:
                    blocks.append(
                        OcrBlock(
                            parsing_version=version,
                            document_page=document_page,
                            reading_order=region.reading_order,
                            text=region.text,
                            polygon=original_polygon(page.page_number, region.polygon),
                            layout_polygon=region.polygon,
                            confidence=region.confidence,
                            provider_metadata=dict(page.provider_metadata),
                        )
                    )
            OcrBlock.objects.bulk_create(blocks)

            evidence_rows = []
            observation_rows = []
            for observation in observations:
                document_page = document_pages[observation.page_number]
                evidence = SourceEvidence(
                    parsing_version=version,
                    document_page=document_page,
                    polygon=original_polygon(observation.page_number, observation.region),
                    source_text=observation.source_text,
                    confidence=observation.confidence,
                )
                evidence_rows.append(evidence)
                observation_rows.append(
                    LabObservation(
                        parsing_version=version,
                        document_page=document_page,
                        evidence=evidence,
                        reading_order=observation.reading_order,
                        raw_name=observation.raw_name,
                        standard_code=observation.standard_code,
                        standard_name=observation.standard_name,
                        raw_value=observation.raw_value,
                        result_type=observation.result_type,
                        raw_unit=observation.raw_unit,
                        reference_range_raw=observation.reference_range_raw,
                        report_flag_raw=observation.report_flag_raw,
                        observation_date=page_contexts[observation.page_number]['observation_date'],
                        institution_raw=page_contexts[observation.page_number]['institution_raw'],
                        capability_level=observation.capability_level,
                        dictionary_version=observation.dictionary_version,
                        specimen=observation.specimen,
                        method_raw=observation.method_raw,
                        field_evidence=original_fields({**observation.field_evidence, 'observation_date': page_contexts[observation.page_number]['date_evidence']}),
                        quality_issues=list(observation.quality_issues),
                        normalization_candidates=list(observation.normalization_candidates),
                        reference_range=dict(observation.reference_range),
                        quality_rule_version=VALIDATION_RULE_VERSION,
                    )
                )
            SourceEvidence.objects.bulk_create(evidence_rows)
            LabObservation.objects.bulk_create(observation_rows)

            metadata_evidence = []
            metadata_rows = []
            for candidate in metadata_candidates:
                evidence = None
                if candidate.page_number is not None and candidate.region is not None:
                    evidence = SourceEvidence(
                        parsing_version=version,
                        document_page=document_pages[candidate.page_number],
                        polygon=original_polygon(candidate.page_number, candidate.region),
                        source_text=candidate.raw_text,
                        confidence=candidate.confidence,
                    )
                    metadata_evidence.append(evidence)
                metadata_rows.append(
                    DocumentMetadataCandidate(
                        parsing_version=version,
                        kind=candidate.kind,
                        raw_text=candidate.raw_text,
                        normalized_value=candidate.normalized_value,
                        precision=candidate.precision,
                        confidence=candidate.confidence,
                        evidence=evidence,
                        selected=candidate.selected,
                        rationale=dict(candidate.rationale),
                    )
                )
            SourceEvidence.objects.bulk_create(metadata_evidence)
            DocumentMetadataCandidate.objects.bulk_create(metadata_rows)
            DocumentSummary.objects.create(
                parsing_version=version,
                document_type=metadata.document_type,
                document_date_raw=metadata.document_date_raw,
                document_date=metadata.document_date,
                date_precision=metadata.date_precision,
                institution_raw=metadata.institution_raw,
                confidence=metadata.confidence,
            )
            version.status = ParsingVersionStatus.READY
            try:
                extract_version_facts(version)
            except Exception:
                # The extractor has its own savepoint: no partial facts become available.
                logging.getLogger(__name__).warning("Fact extraction failed; original retained", extra={"error_code": "fact_extraction_failed"})
                FactExtraction.objects.create(
                    parsing_version=version, status="FAILED", extractor_version=EXTRACTOR_VERSION,
                    reason="extraction_failed",
                )
            from apps.facts.clinical_extraction import EXTRACTOR_VERSION as CLINICAL_EXTRACTOR_VERSION, extract_clinical_version
            from apps.facts.clinical_schema import SCHEMA_VERSION as CLINICAL_SCHEMA_VERSION
            try:
                extract_clinical_version(version, construction_context=context)
            except Exception:
                # An independent savepoint preserves legacy extraction and the
                # original even if the structured extractor fails completely.
                logging.getLogger(__name__).warning("Clinical field extraction failed; original retained",
                                                    extra={"error_code": "clinical_extraction_failed"})
                ClinicalExtraction.objects.create(
                    parsing_version=version, status="FAILED", extractor_version=CLINICAL_EXTRACTOR_VERSION,
                    schema_version=CLINICAL_SCHEMA_VERSION, reason="extraction_failed",
                )
            from apps.labs.dictionary_workflow import collect_dictionary_candidates

            collect_dictionary_candidates(version)
            version.diagnostics = {
                "material": {**(material or {}), "source_sha256": document.sha256},
                "quality_policy": QUALITY_POLICY_VERSION,
                "validation_rule_version": VALIDATION_RULE_VERSION,
                "document_type": metadata.document_type,
                "observation_count": len(observations),
                "ocr_block_count": len(blocks),
                "page_count": len(pages),
                "preparation_warnings": sorted(set(warnings)),
                "preparation_pages": {
                    str(page.page_number): {
                        **page.preparation_metadata,
                        "source_transform": page.source_transform,
                        "layout_size": [page.width, page.height],
                        "source_mapping_unavailable": any(original_polygon(page.page_number, region.polygon) is None for region in page.regions),
                    } for page in pages
                },
            }
            version.diagnostics['validation'] = {
                str(row.pk): list(validate_observation(row, previous=observation_rows, dictionary=self.dictionary))
                for row in observation_rows
            }
            version.save(update_fields=["status", "diagnostics", "updated_at"])
            from apps.cancer_ordering.extraction import collect_processing_version

            collect_processing_version(context, version)


_PROVIDER_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _process_provider(pid, provider_class, detection_model, recognition_model, detection_dir, recognition_dir, device):
    # Include the PID so a prefork child never uses an engine loaded by its parent.
    return provider_class(
        detection_model=detection_model,
        recognition_model=recognition_model,
        detection_model_dir=detection_dir,
        recognition_model_dir=recognition_dir,
        device=device,
    )


def build_default_pipeline():
    with _PROVIDER_LOCK:
        provider = _process_provider(
            os.getpid(), PaddleOcrProvider,
            settings.PHR_OCR_PADDLE_DETECTION_MODEL,
            settings.PHR_OCR_PADDLE_RECOGNITION_MODEL,
            settings.PHR_OCR_PADDLE_DETECTION_MODEL_DIR or None,
            settings.PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR or None,
            settings.PHR_OCR_DEVICE,
        )
    return DocumentProcessingPipeline(
        object_store=get_object_store(),
        raster_provider=provider,
        dictionary=current_dictionary(),
    )
