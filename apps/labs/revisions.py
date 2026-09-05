"""Immutable transcription edits and one effective-result resolver for all read paths."""

from copy import copy, deepcopy
from datetime import date
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate

from .dictionary import current_dictionary
from .extraction import _result_type
from .models import LabObservation, ObservationRevision, RevisionAction


class RevisionConflict(ValueError):
    pass


VALUE_FIELDS = (
    "raw_name", "standard_code", "standard_name", "raw_value", "raw_unit", "result_type",
    "observation_date", "specimen", "capability_level", "method_raw", "reference_range_raw",
)
EDITABLE_FIELDS = frozenset({"raw_name", "standard_code", "raw_value", "raw_unit", "observation_date"})
STATE_FIELDS = {
    "review_state", "reported_error", "resolved_issues", "value_origin", "revision_conflict",
    "revision_conflicts", "date_verified", "mapping_dictionary_version", "value_sources",
}


def _source_identity(observation, revision_id=None):
    return {"observation_id": str(observation.pk), "evidence_id": str(observation.evidence_id),
            "revision_id": str(revision_id) if revision_id else None}


def _snapshot(observation):
    values = {name: getattr(observation, name) for name in VALUE_FIELDS}
    if values["observation_date"] is not None:
        values["observation_date"] = values["observation_date"].isoformat()
    values.update(
        review_state=getattr(observation, "review_state", "AUTOMATIC"),
        reported_error=getattr(observation, "reported_error", False),
        resolved_issues=list(getattr(observation, "resolved_issues", ())),
        value_origin=getattr(observation, "value_origin", "AUTOMATIC"),
        revision_conflict=getattr(observation, "revision_conflict", False),
        revision_conflicts=deepcopy(getattr(observation, "revision_conflicts", [])),
        value_sources=deepcopy(getattr(observation, "value_sources", {
            name: _source_identity(observation) for name in VALUE_FIELDS
        })),
        date_verified=getattr(observation, "date_verified", False),
        mapping_dictionary_version=getattr(observation, "mapping_dictionary_version", observation.dictionary_version),
    )
    return values


def _latest_revision(observation):
    return observation.revisions.order_by("-sequence").first()


def _inherited_observation(observation):
    """Only earlier versions can contribute edits; ambiguous identities need explicit reconciliation."""
    from apps.processing.models import ParsingVersion

    version_id = observation.parsing_version_id
    visited = set()
    while version_id and version_id not in visited:
        visited.add(version_id)
        version_id = ParsingVersion.objects.filter(
            pk=version_id, document_id=observation.parsing_version.document_id,
        ).values_list("previous_version_id", flat=True).first()
        if version_id is None:
            break
        candidates = list(LabObservation.objects.filter(
            parsing_version_id=version_id, revision_number__gt=0,
        ).select_related("evidence", "document_page", "parsing_version").order_by("pk"))
        exact = [item for item in candidates if item.document_page_id == observation.document_page_id
                 and item.evidence.polygon == observation.evidence.polygon]
        current = list(LabObservation.objects.filter(
            parsing_version_id=observation.parsing_version_id,
        ).select_related("evidence"))
        exact_current = [item for item in current if item.document_page_id == observation.document_page_id
                         and item.evidence.polygon == observation.evidence.polygon]
        if len(exact) == len(exact_current) == 1:
            return exact[0]
        matching = [item for item in candidates if observation.standard_code in {
            item.standard_code, _latest_revision(item).after.get("standard_code"),
        }]
        if len(matching) == 1 and sum(item.standard_code == observation.standard_code for item in current) == 1:
            return matching[0]
        if exact or matching:
            break
    return None


def effective_observation(observation):
    """Return a display copy. Never save this copy or mutate the automatic observation."""
    effective = copy(observation)
    effective._state = copy(observation._state)
    effective._state.fields_cache = dict(observation._state.fields_cache)
    effective.quality_issues = deepcopy(observation.quality_issues)
    effective.original_observation_id = observation.pk
    effective.automatic_value = observation.raw_value
    effective.automatic_unit = observation.raw_unit
    effective.review_state = "AUTOMATIC"
    effective.reported_error = False
    effective.resolved_issues = []
    effective.value_origin = "AUTOMATIC"
    effective.revision_conflict = False
    effective.revision_conflicts = []
    effective.value_sources = {name: _source_identity(observation) for name in VALUE_FIELDS}
    effective.date_verified = False
    effective.mapping_dictionary_version = observation.dictionary_version
    revision = _latest_revision(observation)
    inherited = None
    if revision is None:
        inherited = _inherited_observation(observation)
        if inherited is not None:
            revision = _latest_revision(inherited)
    if revision is not None:
        for name, value in revision.after.items():
            if name in VALUE_FIELDS or name in STATE_FIELDS:
                if name == "observation_date":
                    value = date.fromisoformat(value) if value else None
                setattr(effective, name, deepcopy(value))
        effective.applied_revision = revision
        if inherited is not None:
            disagreements = [field for field in VALUE_FIELDS
                             if getattr(inherited, field) != getattr(observation, field)]
            if (inherited.document_page_id != observation.document_page_id
                    or inherited.evidence.polygon != observation.evidence.polygon
                    or inherited.evidence.source_text != observation.evidence.source_text):
                disagreements.append("source")
            effective.revision_conflicts = sorted(set(effective.revision_conflicts) | set(disagreements))
            effective.revision_conflict = effective.revision_conflict or bool(disagreements)
        if "value_sources" not in revision.after:
            effective.value_sources = {name: _source_identity(revision.observation, revision.pk) for name in VALUE_FIELDS}
        origin = effective.value_sources["raw_value"]
        effective.original_observation_id = UUID(origin["observation_id"])
        if origin["evidence_id"] != str(observation.evidence_id):
            from apps.processing.models import SourceEvidence
            effective.carried_source_evidence = SourceEvidence.objects.get(
                pk=origin["evidence_id"], parsing_version__document_id=observation.parsing_version.document_id,
            )
    else:
        effective.applied_revision = None
    # Layout/normalization issues belong to each original field source. A clean
    # replacement parse cannot certify raw fields retained from an older parse.
    source_fields = {}
    for field, source in effective.value_sources.items():
        source_fields.setdefault(source['observation_id'], set()).add(field)
    originals = {str(observation.pk): observation}
    missing = set(source_fields) - set(originals)
    if missing:
        originals.update({str(item.pk): item for item in LabObservation.objects.filter(
            pk__in=missing, parsing_version__document_id=observation.parsing_version.document_id,
        )})
    effective.quality_issues = []
    for source_id, fields in source_fields.items():
        original = originals.get(source_id)
        if original is None:
            continue  # Validation separately marks unavailable field evidence.
        for source_issue in original.quality_issues:
            affected = set(source_issue.get('fields') or VALUE_FIELDS)
            applicable = fields & affected if affected <= set(VALUE_FIELDS) else fields
            if applicable:
                effective.quality_issues.append({**deepcopy(source_issue), 'fields': sorted(applicable)})
    if effective.applied_revision is not None:
        from .dictionary import DictionaryError, dictionary_for_version
        from .validation import TREND_BLOCKING_ISSUES, validate_observation
        try:
            dictionary = dictionary_for_version(effective.mapping_dictionary_version)
        except DictionaryError:
            dictionary = None
        definition = next((item for item in dictionary.indicators if item.code == effective.standard_code), None) if dictionary else None
        if definition and not {item['code'] for item in validate_observation(effective, dictionary=dictionary)} & TREND_BLOCKING_ISSUES:
            effective.capability_level = definition.capability_level.value
    return effective


def lock_observation(observation_id):
    identity = LabObservation.objects.filter(pk=observation_id).values_list(
        "parsing_version__document_id", flat=True,
    ).first()
    if identity is None:
        raise PermissionDenied
    document, _batches = lock_document_aggregate(identity)
    if document is None or document.deleted_at is not None or not document.patient.account.is_active:
        raise PermissionDenied
    observation = LabObservation.objects.select_for_update().select_related(
        "parsing_version", "evidence", "document_page",
    ).filter(pk=observation_id).first()
    if observation is None:
        raise PermissionDenied
    return document, observation


def _checked_changes(effective, changes):
    if not isinstance(changes, dict) or not changes or not set(changes) <= EDITABLE_FIELDS:
        raise ValidationError("请选择日期、项目、结果或单位更正。")
    output = {}
    for name, value in changes.items():
        if not isinstance(value, str):
            raise ValidationError("更正内容必须是文字。")
        value = value.strip()
        limit = LabObservation._meta.get_field(name).max_length or 10
        if len(value) > limit or (name != "raw_unit" and not value):
            raise ValidationError("更正内容为空或过长。")
        output[name] = value
    if "observation_date" in output:
        try:
            parsed_date = date.fromisoformat(output["observation_date"])
        except ValueError:
            raise ValidationError("请输入有效日期。") from None
        if not 1900 <= parsed_date.year <= 2100:
            raise ValidationError("日期超出可整理范围。")
        output["observation_date"] = parsed_date.isoformat()
        output["date_verified"] = True
    if "raw_value" in output:
        kind = _result_type(output["raw_value"])
        if kind is None:
            raise ValidationError("结果须保留报告中的数字、比较符、定性、半定量或状态。")
        output["result_type"] = str(kind)
    if "standard_code" in output or "raw_name" in output:
        dictionary = current_dictionary()
        definition = None
        if "standard_code" in output:
            definition = next((item for item in dictionary.indicators if item.code == output["standard_code"]), None)
        else:
            definition = dictionary.match(output["raw_name"], specimen=effective.specimen)
        if definition is None:
            raise ValidationError("项目尚未明确，请选择正式字典中的项目，或仅反馈识别有误。")
        output.update(
            standard_code=definition.code, standard_name=definition.standard_name,
            capability_level=definition.capability_level.value,
            specimen=getattr(definition, "specimen", "") or effective.specimen,
            mapping_dictionary_version=dictionary.version,
        )
    return output


def append_revision(actor, observation, *, action, changes, expected_revision, origin="USER", resolved_issues=()):
    """Internal operation; callers hold the document and observation locks and authorize the actor."""
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or observation.revision_number != expected_revision):
        raise RevisionConflict("内容已更新，请刷新后重新核对。")
    if not observation.parsing_version.active:
        raise RevisionConflict("解析版本已变化，请打开当前结果。")
    if action not in RevisionAction.values:
        raise ValidationError("未知核对操作。")
    if action != RevisionAction.CORRECT and changes:
        raise ValidationError("只有更正操作可以包含新的字段值。")
    effective = effective_observation(observation)
    before = _snapshot(effective)
    after = deepcopy(before)
    event_id = uuid4()
    if action == RevisionAction.UNDO:
        latest = _latest_revision(observation)
        if latest is None:
            raise ValidationError("没有可撤销的操作。")
        after = deepcopy(latest.before)
    else:
        after["review_state"] = str(action)
        if action == RevisionAction.REPORT_ERROR:
            after["reported_error"] = True
        elif action == RevisionAction.CORRECT:
            checked = _checked_changes(effective, changes)
            after.update(checked)
            for name in set(checked) & set(VALUE_FIELDS):
                after["value_sources"][name] = _source_identity(observation, event_id)
            # A field edit cannot acknowledge unrelated source/reparse differences.
            after.update(value_origin=origin, reported_error=False, resolved_issues=[])
        elif action in {RevisionAction.KEEP_REVISION, RevisionAction.USE_AUTOMATIC}:
            if not effective.revision_conflict:
                raise ValidationError("当前没有待处理的重解析冲突。")
            if action == RevisionAction.USE_AUTOMATIC:
                after = _snapshot(observation)
                after["review_state"] = str(action)
            after.update(revision_conflict=False, revision_conflicts=[], resolved_issues=[])
        elif action == RevisionAction.CONFIRM:
            after["reported_error"] = False
        if origin == "REVIEW":
            from .validation import REVIEWABLE_ISSUES
            if not set(resolved_issues) <= REVIEWABLE_ISSUES:
                raise ValidationError("复核不能跳过单位、日期或可比性检查。")
            after["resolved_issues"] = sorted(set(after["resolved_issues"]) | set(resolved_issues))
    # Compare-and-swap protects SQLite too; PostgreSQL additionally serializes on row locks.
    updated = LabObservation.objects.filter(pk=observation.pk, revision_number=expected_revision).update(
        revision_number=expected_revision + 1,
    )
    if updated != 1:
        raise RevisionConflict("内容已更新，请刷新后重新核对。")
    event = ObservationRevision.objects.create(
        id=event_id, observation=observation, author=actor, origin=origin, action=action, sequence=expected_revision + 1,
        before=before, after=after, source_evidence=observation.evidence,
    )
    observation.revision_number = expected_revision + 1
    return event


def revise_observation(actor, observation_id, *, action, changes, expected_revision):
    with transaction.atomic():
        document, observation = lock_observation(observation_id)
        if not actor.is_active or document.patient.account_id != actor.pk:
            raise PermissionDenied
        return append_revision(actor, observation, action=action, changes=changes, expected_revision=expected_revision)
