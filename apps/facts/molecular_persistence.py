"""Atomic persistence of source-located molecular and shared legacy candidates."""
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.processing.models import SourceEvidence

from .clinical_context import validate_context_candidate
from .clinical_schema import FIELDS, field_content
from .models import Fact, FactSourceFragment
from .molecular_schema import SCHEMA, CONTEXT, DATE_KEYS, field_allowed_in_report
from .pathology_schema import CONTEXT as IHC_CONTEXT
from .pathology_source import VERSION as SOURCE_VERSION


def persist(report, candidates, *, construction_context=None):
    if report.routing_kind != "MOLECULAR":
        raise ValidationError("分子候选必须处于明确分子报告范围。")
    nodes = {c.node_id: c for c in candidates}
    if len(nodes) != len(candidates):
        raise ValidationError("分子候选临时身份不能重复。")
    persisted = {}
    with transaction.atomic():
        for order, candidate in enumerate(sorted(candidates, key=lambda c: FIELDS[c.key].rank)):
            if not field_allowed_in_report(candidate.key, "MOLECULAR"):
                raise ValidationError("此报告不支持该字段。")
            if candidate.value_fragments is None or candidate.label_fragments is None:
                raise ValidationError("新自动候选须明确自己的值和标签窗口。")
            pieces, positions = [], {}

            def retain(piece):
                identity = piece.block.pk, piece.start, piece.end
                if identity not in positions:
                    positions[identity] = len(pieces)
                    pieces.append(piece)
                return positions[identity]

            literal = [retain(p) for p in candidate.fragments]
            declaration = {"version": SOURCE_VERSION, "literal_fragment_ordinals": literal,
                           "value_fragment_ordinals": [retain(p) for p in candidate.value_fragments],
                           "label_fragment_ordinals": list(dict.fromkeys(retain(p) for p in candidate.label_fragments))}
            if set(candidate.links) != set(FIELDS[candidate.key].roles):
                raise ValidationError("分子候选须显式声明每个必需角色。")
            bindings = []
            molecular = FIELDS[candidate.key].version == SCHEMA
            for role in FIELDS[candidate.key].roles:
                selected = candidate.links[role]
                selected = selected if isinstance(selected, list) else [selected]
                for identity in selected:
                    target = persisted.get(identity) if identity else None
                    if identity is not None and target is None:
                        raise ValidationError("自动关联目标必须是已持久化的低层明确锚。")
                    proof = [retain(p) for p in nodes[identity].value_fragments] if target else []
                    bindings.append({"role": role, "state": "BOUND" if target else "UNKNOWN",
                                     "target_fact_id": str(target.pk) if target else None,
                                     "target_entity_key": target.entity_key if target else None,
                                     "proof_fragment_ordinals": proof, "reason": None if target else "NOT_STATED"})
            context_version = CONTEXT if molecular else IHC_CONTEXT
            context = {"context_version": context_version, "report_id": str(report.pk), "membership_policy": context_version, "bindings": bindings}
            if molecular:
                context["association"] = None
                if candidate.key.startswith("drug_evidence."):
                    context["association"] = {"state": "EXPLICIT" if candidate.association_raw else "UNKNOWN", "raw": candidate.association_raw,
                                              "proof_fragment_ordinals": [retain(p) for p in candidate.association_fragments]}
            if not pieces:
                raise ValidationError("分子自动候选不能没有实际原值片段。")
            raw = "\n".join(p.text for p in pieces)
            first = pieces[0]
            evidence = SourceEvidence.objects.create(parsing_version=report.parsing_version, document_page=first.block.document_page,
                ocr_block=first.block if len(pieces) == 1 else None, polygon=first.block.polygon if len(pieces) == 1 else None,
                source_text=raw, confidence=min(p.block.confidence for p in pieces))
            kwargs = {"entity_context": context, "source_role": candidate.source_role,
                      "limitations": [*report.limitations, *candidate.limitations], "transformations": candidate.transformations}
            if molecular and candidate.assertion_code:
                kwargs["reported_assertion"] = {"code": candidate.assertion_code, "raw": candidate.assertion_raw,
                                                "proof_fragment_ordinals": list(dict.fromkeys(retain(p) for p in candidate.assertion_fragments))}
            if candidate.key in DATE_KEYS:
                from .molecular_adapters import molecular_field_content
                content = molecular_field_content(candidate.key, {**candidate.value, "raw": candidate.raw_value}, **kwargs)
            else:
                content = field_content(candidate.key, candidate.value, candidate.raw_value, **kwargs)
            content["literal_source"] = declaration
            fact = Fact(document=report.document, document_page=first.block.document_page, parsing_version=report.parsing_version,
                        evidence=evidence, origin="AUTOMATIC", category=content["category"], representation="FIELD", clinical_report=report,
                        field_key=candidate.key, entity_key=candidate.entity, schema_version=content["schema_version"], raw_text=raw,
                        automatic_content=content, reading_order=order, created_by=report.created_by)
            fact.full_clean()
            fact.save()
            for ordinal, piece in enumerate(pieces):
                source = evidence if len(pieces) == 1 else SourceEvidence.objects.create(parsing_version=report.parsing_version,
                    document_page=piece.block.document_page, ocr_block=piece.block, polygon=piece.block.polygon,
                    source_text=piece.text, confidence=piece.block.confidence)
                fragment = FactSourceFragment(fact=fact, ordinal=ordinal, document_page=piece.block.document_page,
                    evidence=source, ocr_block=piece.block, source_kind="OCR", start_offset=piece.start, end_offset=piece.end,
                    raw_text=piece.text, polygon=piece.block.polygon)
                fragment.full_clean()
                fragment.save()
            validate_context_candidate(fact, construction_context=construction_context)
            persisted[candidate.node_id] = fact
    return len(persisted)
