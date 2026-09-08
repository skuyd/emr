"""Project actual automatic pathology fields onto a separately verified OCR index.

No gold, reviewed source IDs or expected values enter this module. The caller
verifies the original/cache file bytes; this mapper independently re-reads ORM
fields, fragments, evidence, blocks and actual same-report binding targets.
It performs no label extraction. A label absent from the field's saved source
cannot be repaired from neighbouring text, even if a human can locate it.
All output, including original text and diagnostic identifiers, is private.
"""
from copy import deepcopy
import hashlib
import re

from apps.processing.geometry import IDENTITY_TRANSFORM, source_polygon
from apps.processing.value_objects import InvalidRegion, normalized_polygon


VERSION = "PATHOLOGY_ORIGINAL_MAPPING_V1"


class MappingInputError(ValueError):
    pass


def _compact(text):
    return "".join(c for c in text if not c.isspace())


def _polygon(value):
    try:
        return [list(point) for point in normalized_polygon(value)]
    except (InvalidRegion, ValueError, TypeError):
        return None


def original_pages(fixed_pages, *, source_sha256, ocr_sha256):
    """Keep original cache array order, Unicode and original-space coordinates."""
    if any(not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value) for value in (source_sha256, ocr_sha256)):
        raise MappingInputError("Original and OCR SHA256 must be explicit")
    pages, seen = [], set()
    if not isinstance(fixed_pages, list) or not fixed_pages:
        raise MappingInputError("The complete fixed page list is required")
    for page in fixed_pages:
        number = page.get("page_number")
        if type(number) is not int or number < 1 or number in seen or not isinstance(page.get("regions"), list):
            raise MappingInputError("Invalid or repeated original page")
        seen.add(number)
        regions, orders = [], set()
        for region in page["regions"]:
            order, text = region.get("reading_order"), region.get("text")
            if type(order) is not int or order < 0 or order in orders or not isinstance(text, str):
                raise MappingInputError("Original region needs unique reading order and raw Unicode")
            orders.add(order)
            try:
                polygon = _polygon(source_polygon(page.get("source_transform", IDENTITY_TRANSFORM), region.get("polygon")))
            except (ValueError, TypeError):
                polygon = None
            if not polygon:
                raise MappingInputError("Original polygon cannot be independently established")
            regions.append({"text": text, "reading_order": order, "polygon": polygon})
        pages.append({"source_sha256": source_sha256, "ocr_sha256": ocr_sha256, "page": number, "regions": regions})
    return pages


class _Projection:
    def __init__(self, document, version, originals):
        from apps.facts.models import Fact

        self.document = document
        self.version = version
        self.originals = {page["page"]: page for page in originals}
        self.fields = {str(fact.pk): fact for fact in Fact.objects.filter(
            document=document, parsing_version=version, representation="FIELD", category="PATHOLOGY",
        ).select_related("document_page", "document", "parsing_version", "evidence", "clinical_report").order_by(
            "clinical_report__ordinal", "reading_order", "created_at", "pk")}

    def fragment(self, fact, piece, issues):
        """A database locator is a claim; both stored evidence and cache must agree."""
        from django.core.exceptions import ValidationError

        def invalid(reason):
            issues.append({"severity": "ERROR", "reason": reason, "fragment_ordinal": piece.ordinal})
            return None

        if piece.source_kind != "OCR" or not piece.ocr_block_id:
            return invalid("not_automatic_original_ocr")
        try:
            piece.clean()
        except (ValidationError, ValueError, TypeError, AttributeError):
            return invalid("persisted_fragment_evidence_disagrees")
        block, page = piece.ocr_block, self.originals.get(piece.document_page.page_number)
        if (page is None or block.parsing_version_id != fact.parsing_version_id
                or block.document_page.document_id != self.document.pk):
            return invalid("original_document_or_version_disagrees")
        matches = [(index, region) for index, region in enumerate(page["regions"])
                   if region["reading_order"] == block.reading_order
                   and region["text"].strip() == block.text and _polygon(block.polygon) == region["polygon"]]
        if len(matches) != 1:
            return invalid("fixed_original_block_missing_or_ambiguous")
        index, region = matches[0]
        # OcrRegion strips only the outer whitespace before persistence. Carry
        # that exact shift back to the unmodified cache, never normalize offsets.
        shift = len(region["text"]) - len(region["text"].lstrip())
        start, end = shift + piece.start_offset, shift + piece.end_offset
        if region["text"][start:end] != piece.raw_text:
            return invalid("original_character_span_disagrees")
        return {key: deepcopy(page[key]) for key in ("source_sha256", "ocr_sha256", "page")} | {
            "region_index": index, "reading_order": region["reading_order"],
            "start_offset": start, "end_offset": end, "raw_text": piece.raw_text,
            "polygon": deepcopy(region["polygon"]), "full_block_text_sha256": hashlib.sha256(region["text"].encode("utf-8")).hexdigest()}

    def node(self, fact, stack=()):
        from django.core.exceptions import ValidationError
        from apps.facts.clinical_context import validate_context_candidate
        from apps.facts.models import FactSourceFragment
        from apps.facts.pathology_schema import TARGET_KEYS
        from apps.facts.clinical_schema import FIELDS

        identity = str(fact.pk)
        issues = []
        content = deepcopy(fact.automatic_content) if isinstance(fact.automatic_content, dict) else {}
        item = {"candidate_id": identity, "field_key": fact.field_key,
                "value": content.get("value"), "source_role": content.get("source_role"),
                "value_evidence": [], "label_evidence": [], "bindings": {}, "mapping_diagnostics": issues,
                "production_identity": {"report_id": str(fact.clinical_report_id), "entity_key": fact.entity_key,
                    "parsing_version_id": str(fact.parsing_version_id), "origin": fact.origin,
                    "revision_number": fact.revision_number, "schema_version": fact.schema_version,
                    "raw_value": content.get("raw_value"), "raw_text": fact.raw_text,
                    "entity_context": content.get("entity_context")}}
        if identity in stack or len(stack) >= 8:
            issues.append({"severity": "ERROR", "reason": "cyclic_or_excessive_context"})
            return item
        try:
            fact.clean()
            validate_context_candidate(fact)
        except (ValidationError, ValueError, TypeError, AttributeError, KeyError):
            issues.append({"severity": "ERROR", "reason": "persisted_candidate_or_context_invalid"})
        if fact.origin != "AUTOMATIC" or fact.revision_number or fact.revisions.exists():
            issues.append({"severity": "ERROR", "reason": "not_an_unrevised_automatic_prediction"})
        # No caller-prefetched fragment, block, evidence or target is trusted.
        pieces = list(FactSourceFragment.objects.filter(fact=fact).select_related(
            "fact__clinical_report", "document_page", "evidence", "ocr_block__document_page").order_by("ordinal"))
        proofs = {piece.ordinal: self.fragment(fact, piece, issues) for piece in pieces}
        if [piece.ordinal for piece in pieces] != list(range(len(pieces))):
            issues.append({"severity": "ERROR", "reason": "noncontiguous_fragment_ordinals"})
        # persist_pathology_candidates saves the candidate's own pieces first,
        # then de-duplicated binding proofs. Find its exact literal prefix; do
        # not search other rows for a value matching an evaluation answer.
        raw, own, prefix = content.get("raw_value"), [], ""
        if isinstance(raw, str) and _compact(raw):
            for piece in pieces:
                prefix += _compact(piece.raw_text)
                own.append(proofs[piece.ordinal])
                if prefix == _compact(raw):
                    break
                if not _compact(raw).startswith(prefix):
                    own = []
                    break
            else:
                own = []
        if not own or any(proof is None for proof in own):
            issues.append({"severity": "ERROR", "reason": "own_literal_source_prefix_unverified"})
        else:
            item["value_evidence"] = deepcopy(own)
            # This is an evidence window, not a claim that any missing label
            # was found. The independent scorer checks its required raw label.
            item["label_evidence"] = deepcopy(own)
        context = content.get("entity_context")
        bindings = context.get("bindings") if isinstance(context, dict) else None
        if not isinstance(bindings, list):
            issues.append({"severity": "ERROR", "reason": "bindings_missing"})
            return item
        for binding in bindings:
            if not isinstance(binding, dict) or not isinstance(binding.get("role"), str):
                issues.append({"severity": "ERROR", "reason": "binding_shape_invalid"})
                continue
            role = binding["role"]
            if role in item["bindings"]:
                issues.append({"severity": "ERROR", "reason": "duplicate_binding_role", "role": role})
                continue
            actual = {"state": binding.get("state"), "reason": binding.get("reason"), "target": None, "proof_evidence": []}
            item["bindings"][role] = actual
            ordinals = binding.get("proof_fragment_ordinals")
            if not isinstance(ordinals, list) or any(type(i) is not int or i not in proofs or proofs[i] is None for i in ordinals):
                issues.append({"severity": "ERROR", "reason": "binding_proof_unverified", "role": role})
            else:
                actual["proof_evidence"] = [deepcopy(proofs[i]) for i in ordinals]
            if binding.get("state") == "UNKNOWN":
                continue
            target = self.fields.get(binding.get("target_fact_id"))
            if (target is None or str(target.pk) in (*stack, identity) or len(stack) >= 7
                    or target.clinical_report_id != fact.clinical_report_id
                    or target.field_key != TARGET_KEYS.get(role) or target.field_key not in FIELDS or fact.field_key not in FIELDS
                    or FIELDS[target.field_key].rank >= FIELDS[fact.field_key].rank
                    or target.entity_key != binding.get("target_entity_key")):
                issues.append({"severity": "ERROR", "reason": "actual_binding_target_unverified", "role": role})
                continue
            actual["target"] = self.node(target, (*stack, identity))
        return item


def map_document(document_id, fixed_pages, *, source_sha256, ocr_sha256, execution_status):
    """Reload one completed isolated document, retaining every pathology FIELD.

    Non-pathology FIELDs and EXCERPTs are counted separately. An extraction or
    page failure cannot become a successful empty page. Malformed candidates
    stay in input order with diagnostics; runtime UUIDs are never a match key.
    """
    from apps.documents.models import Document
    from apps.facts.models import ClinicalExtraction

    originals = original_pages(fixed_pages, source_sha256=source_sha256, ocr_sha256=ocr_sha256)
    numbers = {page["page"] for page in originals}
    if (not isinstance(execution_status, dict) or set(execution_status) != numbers
            or any(type(key) is not int or value not in {"COMPLETE", "FAILED", "NOT_RUN"} for key, value in execution_status.items())):
        raise MappingInputError("Every original page needs its own explicit execution status")
    document = Document.objects.get(pk=document_id)
    if document.sha256 != source_sha256:
        raise MappingInputError("Actual document and original source identity disagree")
    version = document.parsing_versions.filter(active=True, status="PUBLISHED", published_at__isnull=False).first()
    extraction = ClinicalExtraction.objects.filter(parsing_version=version).first() if version else None
    clinical_status = extraction.status if extraction else "NOT_RUN"
    projection = _Projection(document, version, originals)
    pages = {page["page"]: {key: deepcopy(page[key]) for key in ("source_sha256", "ocr_sha256", "page")} | {
        "status": execution_status[page["page"]], "pipeline_status": execution_status[page["page"]],
        "clinical_status": clinical_status, "items": [], "input_region_count": len(page["regions"])} for page in originals}
    for page in pages.values():
        if page["status"] == "COMPLETE" and clinical_status not in {"EXTRACTED", "NO_REPORTS", "PARTIAL"}:
            page["status"] = "FAILED" if clinical_status == "FAILED" else "NOT_RUN"
    unattributable = []
    for fact in projection.fields.values():
        try:
            item = projection.node(fact)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as error:
            # A malformed persisted candidate is still an actual candidate.
            # Do not replace a mapping failure with an empty successful page.
            content = fact.automatic_content if isinstance(fact.automatic_content, dict) else {}
            item = {"candidate_id": str(fact.pk), "field_key": fact.field_key, "value": deepcopy(content.get("value")),
                    "source_role": content.get("source_role"), "value_evidence": [], "label_evidence": [], "bindings": {},
                    "production_identity": {"automatic_content": deepcopy(fact.automatic_content), "raw_text": fact.raw_text},
                    "mapping_diagnostics": [{"severity": "ERROR", "reason": "projection_exception", "error_type": type(error).__name__}]}
        page = pages.get(fact.document_page.page_number)
        if page is None:
            # Retain the invalid envelope so the scorer also accounts for it.
            unattributable.append({"source_sha256": source_sha256, "ocr_sha256": ocr_sha256,
                                   "page": fact.document_page.page_number, "status": "INVALID", "items": [item]})
        else:
            page["items"].append(item)
            if any(issue.get("reason") == "projection_exception" for issue in item["mapping_diagnostics"]):
                page["status"] = "FAILED"
    return {"pages": [*pages.values(), *unattributable], "receipt": {
        "mapper_version": VERSION, "document_id": str(document.pk), "parsing_version_id": str(version.pk) if version else None,
        "dictionary_version": version.dictionary_version if version else None, "dictionary_hash": version.dictionary_hash if version else None,
        "clinical_status": clinical_status, "clinical_reason": extraction.reason if extraction else "",
        "clinical_unparsed_page_count": extraction.unparsed_page_count if extraction else None,
        "candidate_count": len(projection.fields), "unattributable_candidates": len(unattributable),
        "other_fact_count": document.facts.filter(parsing_version=version).count() - len(projection.fields)}}
