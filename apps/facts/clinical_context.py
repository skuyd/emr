"""Bounded same-report dependency reads for immutable pathology/IHC fields.

The resolver is local to one authorized read/transaction. It is never a shared
cache: new members, author erasure and every revision head remain observable.
"""
from copy import deepcopy
import unicodedata

from django.core.exceptions import ValidationError

from .clinical_schema import FIELDS, validate_content
from .models import Fact
from .pathology_schema import CONTEXT, MEMBER_KEYS, SCHEMA, TARGET_KEYS
from .readmodels import digest


def has_context(fact):
    return fact.representation == "FIELD" and fact.schema_version == SCHEMA


def _literal(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value) if not c.isspace())


def _binding_identity(binding):
    return binding["state"], binding["target_fact_id"], binding["target_entity_key"]


class ContextResolver:
    MAX_NODES = 32
    MAX_DEPTH = 8

    def __init__(self, report):
        from .clinical_readmodels import report_state

        self.report = report
        self.report_state = report_state(report)
        self.fields = {str(f.pk): f for f in Fact.objects.filter(clinical_report_id=report.pk, representation="FIELD").select_related(
            "document__patient__account", "document_page", "parsing_version", "evidence",
        ).prefetch_related("source_fragments__ocr_block", "source_fragments__document_page", "source_fragments__evidence", "revisions")}
        for field in self.fields.values():
            field.clinical_report = report
        self._evaluated = {}

    def _revisions(self, fact):
        return sorted(fact.revisions.all(), key=lambda revision: revision.sequence)

    def _state(self, fact):
        revisions = self._revisions(fact)
        return deepcopy(revisions[-1].after) if revisions else {"content": deepcopy(fact.automatic_content), "status": "PENDING"}

    def _bindings(self, fact):
        validate_content(fact.automatic_content, field_key=fact.field_key)
        context = fact.automatic_content["entity_context"]
        if context["report_id"] != str(self.report.pk) or fact.clinical_report_id != self.report.pk:
            raise ValidationError("上下文必须属于字段自己的报告。")
        return {binding["role"]: binding for binding in context["bindings"]}

    def _members(self, fact, bindings):
        if FIELDS[fact.field_key].rank < 30:
            return []
        entities = {binding["target_entity_key"] for role, binding in bindings.items()
                    if role in {"SPECIMEN", "ASSAY"} and binding["state"] == "BOUND"}
        return sorted((field for field in self.fields.values() if field.entity_key in entities and field.field_key in MEMBER_KEYS),
                      key=lambda field: str(field.pk))

    def _links(self, fact, *, fragments=None):
        bindings = self._bindings(fact)
        pieces = list(fact.source_fragments.all()) if fragments is None else fragments
        by_ordinal = {piece.ordinal: piece for piece in pieces}
        targets = {}
        for role, binding in bindings.items():
            if not set(binding["proof_fragment_ordinals"]) <= by_ordinal.keys():
                raise ValidationError("关联依据不属于本字段的实际原文片段。")
            if binding["state"] == "UNKNOWN":
                continue
            target = self.fields.get(binding["target_fact_id"])
            if target is None:
                raise ValidationError("关联目标不存在或不属于本报告。")
            if (target.pk == fact.pk or target.field_key != TARGET_KEYS[role]
                    or target.entity_key != binding["target_entity_key"] or not has_context(target)
                    or target.document_id != fact.document_id or target.document.patient_id != fact.document.patient_id
                    or target.parsing_version_id != fact.parsing_version_id
                    or FIELDS[target.field_key].rank >= FIELDS[fact.field_key].rank):
                raise ValidationError("上下文引用类型、层级或原件范围不一致。")
            if ((role == "SPECIMEN" and FIELDS[fact.field_key].entity_kind == "specimen")
                    or (role == "ASSAY" and FIELDS[fact.field_key].entity_kind == "assay")
                    or role == "MARKER") and target.entity_key != fact.entity_key:
                raise ValidationError("字段必须引用自己所在实体的锚。")
            proof = [by_ordinal[i] for i in binding["proof_fragment_ordinals"]]
            if fact.origin == "AUTOMATIC":
                # Copying a same-named marker elsewhere on the page is not a
                # binding. The field must retain this actual anchor's location.
                target_pieces = list(target.source_fragments.all())
                if not any(p.ocr_block_id and p.ocr_block_id == t.ocr_block_id
                           and p.start_offset <= t.start_offset and p.end_offset >= t.end_offset
                           for p in proof for t in target_pieces):
                    raise ValidationError("自动关联须保留目标锚的原始位置依据。")
            else:
                anchor = target.automatic_content["value"].get("raw", "")
                if not anchor or _literal(anchor) not in _literal("\n".join(p.raw_text for p in proof)):
                    raise ValidationError("人工关联请同时转录原件中明确标本、检测或标记的身份。")
            targets[role] = target
        for role, target in targets.items():
            parent_bindings = self._bindings(target)
            for ancestor in {"SPECIMEN", "ASSAY"} & bindings.keys() & parent_bindings.keys():
                if _binding_identity(bindings[ancestor]) != _binding_identity(parent_bindings[ancestor]):
                    raise ValidationError("字段的标本/检测与其锚的父关联矛盾，不能推断替换。")
        return bindings, targets, self._members(fact, bindings)

    def validate_candidate(self, fact, *, fragments=None):
        """Called after actual fragments exist, within the document transaction."""
        self.fields[str(fact.pk)] = fact
        seen, visiting = set(), set()

        def visit(node, depth, own_fragments=None):
            identity = str(node.pk)
            if identity in visiting or depth > self.MAX_DEPTH:
                raise ValidationError("上下文引用存在循环或超过允许深度。")
            if identity in seen:
                return
            seen.add(identity)
            if len(seen) - 1 > self.MAX_NODES:
                raise ValidationError("上下文依赖过多，请拆分明确范围后核对。")
            visiting.add(identity)
            _, targets, members = self._links(node, fragments=own_fragments)
            for target in targets.values():
                if self._state(target)["status"] == "EXCLUDED" or not self._valid_source(target):
                    raise ValidationError("关联目标已排除或来源不可用。")
            for target in [*targets.values(), *members]:
                if FIELDS[target.field_key].rank >= FIELDS[node.field_key].rank:
                    raise ValidationError("上下文依赖必须指向更低层字段。")
                visit(target, depth + 1)
            visiting.remove(identity)

        visit(fact, 0, fragments)

    def _valid_source(self, fact):
        if not self.report_state["source_valid"] or self.report_state["status"] == "EXCLUDED":
            return False
        fragments = list(fact.source_fragments.all())
        if not fragments:
            return False
        try:
            for fragment in fragments:
                fragment.clean()
        except (ValidationError, ValueError, TypeError, AttributeError):
            return False
        return fact.origin != "AUTOMATIC" or bool(fact.evidence_id and fact.evidence.source_text == fact.raw_text)

    def evaluate(self, fact):
        # Always use this resolver's fresh persisted heads, not a caller's
        # stale related-object cache. Unsaved candidates use validate_candidate.
        try:
            return self._evaluate(self.fields.get(str(fact.pk), fact), [], set())
        except ValidationError as exc:
            # Fail visibly on a graph limit; never use a truncated dependency
            # list as a successful proof. This token only identifies the error.
            reason = "；".join(exc.messages)
            identities = [(key, field.revision_number, field.automatic_content,
                           [(str(r.pk), r.sequence, str(r.author_id) if r.author_id else None) for r in self._revisions(field)])
                          for key, field in sorted(self.fields.items())]
            snapshot = {"context_version": CONTEXT, "binding_state": "INVALID", "error": reason,
                        "membership": [], "dependency_heads": [], "semantic_qualifiers": {}}
            return {"token": digest({"invalid_context": identities, "report": self.report_state["current_source_token"], "error": reason}),
                    "state": "INVALID", "qualified": False, "reason": reason, "snapshot": snapshot, "semantic_qualifiers": {}}

    def _evaluate(self, fact, stack, visited):
        from .clinical_readmodels import base_field_source_token

        identity = str(fact.pk)
        if identity in stack or len(stack) > self.MAX_DEPTH:
            raise ValidationError("上下文引用存在循环或超过允许深度。")
        visited.add(identity)
        if identity in self._evaluated:
            cached = self._evaluated[identity]
            visited.update(head["fact_id"] for head in cached["snapshot"]["dependency_heads"])
            if len(visited) - 1 > self.MAX_NODES:
                raise ValidationError("上下文依赖过多。")
            return cached
        if len(visited) - 1 > self.MAX_NODES:
            raise ValidationError("上下文依赖过多。")
        # No cross-root memo bypass of the root's graph budget.
        state, error, targets, members, bindings = "RESOLVED", "", {}, [], {}
        try:
            bindings, targets, members = self._links(fact)
        except (ValidationError, KeyError, TypeError, ValueError):
            state, error = "INVALID", "字段关联或来源结构无效，请重新核对范围。"
        heads = {}
        for target in sorted({str(t.pk): t for t in [*targets.values(), *members]}.values(), key=lambda t: str(t.pk)):
            if FIELDS[target.field_key].rank >= FIELDS[fact.field_key].rank:
                state, error = "INVALID", "上下文引用层级无效。"
                continue
            resolved = self._evaluate(target, [*stack, identity], visited)
            heads[str(target.pk)] = resolved["head"]
            heads.update({head["fact_id"]: head for head in resolved["snapshot"]["dependency_heads"]})
        if state != "INVALID":
            for role, binding in bindings.items():
                if binding["state"] == "UNKNOWN":
                    state, error = "UNLINKED", "原文已保留，检测/标本或标记未关联。"
                else:
                    head = heads[str(targets[role].pk)]
                    if not head["source_valid"] or head["context_state"] == "INVALID":
                        state, error = "INVALID", "关联来源不可用。"
                        break
                    if head["recorded_status"] == "EXCLUDED":
                        state, error = "EXCLUDED", "必要的关联锚已排除。"
                        break
                    if not head["usable"] and state == "RESOLVED":
                        state, error = "UNREVIEWED", "关联锚尚未完成当前来源核对。"
        revisions = self._revisions(fact)
        recorded = self._state(fact)
        membership = [{"fact_id": str(member.pk), "field_key": member.field_key, "entity_key": member.entity_key,
                       "revision_number": member.revision_number, "latest_revision_id": heads[str(member.pk)]["latest_revision_id"]}
                      for member in members if str(member.pk) in heads]
        snapshot = {"context_version": CONTEXT, "binding_state": state,
                    "bindings": deepcopy(fact.automatic_content.get("entity_context", {}).get("bindings")),
                    "membership": membership, "dependency_heads": [heads[key] for key in sorted(heads)],
                    "report_head": {"revision_number": self.report.revision_number,
                                    "revision_id": self.report_state["revision_id"], "status": self.report_state["status"],
                                    "created_by_id": str(self.report.created_by_id) if self.report.created_by_id else None,
                                    "revision_author_tuples": [(str(r.pk), r.sequence, r.action, str(r.author_id) if r.author_id else None)
                                                               for r in self.report.revisions.order_by("sequence")]}}
        base_token = base_field_source_token(fact, report_token=self.report_state["current_source_token"])
        token = digest({"source": base_token, "context": snapshot})
        source_valid = self._valid_source(fact)
        unchanged = not revisions or recorded.get("source_token") == token
        allowed_role = recorded["content"].get("source_role") in {"CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"}
        if fact.field_key in {"ihc.score", "ihc.result", "ihc.marker"}:
            allowed_role = recorded["content"].get("source_role") == "CURRENT_RESULT"
        usable = recorded["status"] == "CONFIRMED" and unchanged and source_valid and state == "RESOLVED" and allowed_role
        qualifiers = {}
        if fact.field_key in {"ihc.score", "ihc.result"}:
            marker = targets.get("MARKER")
            marker_value = self._state(marker)["content"]["value"] if marker else None
            qualifiers = {"marker": {"code": marker_value["code"], "label": marker_value["label"]} if marker_value else None,
                          "source_role": recorded["content"].get("source_role"),
                          "qualitative_result": recorded["content"]["value"].get("assertion", "NOT_STATED"),
                          "specimen_entity_key": targets["SPECIMEN"].entity_key if "SPECIMEN" in targets else None,
                          "assay_entity_key": targets["ASSAY"].entity_key if "ASSAY" in targets else None,
                          "binding_state": state}
        # Qualifiers include this field's reviewed assertion. Its own revision
        # is an output head, not a source dependency; adding this after hashing
        # avoids a correction invalidating itself. Bound marker heads are hashed.
        snapshot["semantic_qualifiers"] = qualifiers
        head = {"fact_id": identity, "field_key": fact.field_key, "entity_key": fact.entity_key, "schema_version": fact.schema_version,
                "automatic_source_digest": base_token, "effective_content_digest": digest(recorded["content"]),
                "revision_number": fact.revision_number, "latest_revision_id": str(revisions[-1].pk) if revisions else None,
                "recorded_status": recorded["status"], "effective_source_token": token, "source_valid": source_valid,
                "context_state": state, "usable": usable, "created_by_id": str(fact.created_by_id) if fact.created_by_id else None,
                "revision_author_tuples": [(str(r.pk), r.sequence, r.action, str(r.author_id) if r.author_id else None) for r in revisions]}
        result = {"token": token, "state": state, "reason": error or ("此原文属于历史、对照或说明，不作为本次结果。" if not allowed_role else ""),
                "qualified": state == "RESOLVED" and allowed_role, "snapshot": snapshot,
                "semantic_qualifiers": qualifiers, "head": head}
        self._evaluated[identity] = result
        return result


def validate_context_candidate(fact):
    if has_context(fact):
        ContextResolver(fact.clinical_report).validate_candidate(fact)
