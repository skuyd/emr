"""Immutable transcription edits and one effective-result resolver for all read paths."""

from copy import copy, deepcopy
from datetime import date
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from apps.documents.locking import lock_document_aggregate
from apps.patients.access import authorize_patient

from .dictionary import current_dictionary
from .extraction import _result_type
from .models import LabObservation, ObservationRevision, ResultType, RevisionAction


class RevisionConflict(ValueError):
    pass


VALUE_FIELDS = (
    "raw_name", "standard_code", "standard_name", "raw_value", "raw_unit", "result_type",
    "observation_date", "specimen", "capability_level", "method_raw", "reference_range_raw",
    "report_flag_raw", "physiological_phase", "phase_raw",
)
EDITABLE_FIELDS = frozenset({"raw_name", "standard_code", "raw_value", "raw_unit", "observation_date",
                             "physiological_phase", "reference_range_raw", "report_flag_raw", "specimen", "method_raw"})
STATE_FIELDS = {
    "review_state", "reported_error", "resolved_issues", "value_origin", "revision_conflict",
    "revision_conflicts", "date_verified", "mapping_dictionary_version", "value_sources",
    "excluded", "exclusion_reason", "manual_conflict",
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
        excluded=getattr(observation, "excluded", False),
        exclusion_reason=getattr(observation, "exclusion_reason", ""),
        manual_conflict=getattr(observation, "manual_conflict", ""),
    )
    return values


def _latest_revision(observation):
    if getattr(observation, '_read_snapshot', False) and observation.revision_number == 0:
        return None
    return observation.revisions.order_by("-sequence").first()


def _inherited_observation(observation):
    """Only earlier versions can contribute edits; ambiguous identities need explicit reconciliation."""
    from apps.processing.models import ParsingVersion

    if observation.manual_identity:
        # Manual lineage is copied or uniquely linked at activation. Its older
        # revisions remain reachable by identity without replaying them twice.
        return None
    if getattr(observation, '_read_snapshot', False) and observation.parsing_version.previous_version_id is None:
        return None

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
    effective.excluded = False
    effective.exclusion_reason = ""
    effective.manual_conflict = observation.manual_conflict
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
        if observation.manual_identity and observation.parsing_version.previous_version_id:
            ancestors = LabObservation.objects.filter(
                parsing_version_id=observation.parsing_version.previous_version_id,
            ).filter(Q(manual_identity=observation.manual_identity)
                     | Q(manual_counterpart=observation.manual_identity))
            states = [(row, effective_observation(row)) for row in ancestors]
            retained = [state for row, state in states if not state.excluded and not state.manual_conflict]
            inherited = (retained[0] if len(retained) == 1 else
                         next((state for row, state in states
                               if row.manual_identity == observation.manual_identity), None))
            if inherited is not None:
                effective.excluded = inherited.excluded
                effective.exclusion_reason = inherited.exclusion_reason
    for name in ('physiological_phase', 'phase_raw'):
        effective.value_sources.setdefault(name, _source_identity(observation))
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
            # Layout identifiers affect the indicator identity, not the report date.
            if affected & {'row_number', 'project_code', 'row_code'}:
                affected -= {'row_number', 'project_code', 'row_code'}
                affected.update({'raw_name', 'standard_code', 'standard_name'})
            applicable = fields & affected if affected <= set(VALUE_FIELDS) else fields
            if applicable:
                effective.quality_issues.append({**deepcopy(source_issue), 'fields': sorted(applicable)})
    if effective.applied_revision is not None:
        from .catalog_projection import catalog_issues, project_catalog
        from .dictionary import DictionaryError, dictionary_for_version
        from .validation import TREND_BLOCKING_ISSUES, validate_observation
        try:
            dictionary = dictionary_for_version(effective.mapping_dictionary_version)
        except DictionaryError:
            dictionary = None
        definition = next((item for item in dictionary.indicators if item.code == effective.standard_code), None) if dictionary else None
        if definition:
            issues = validate_observation(effective, dictionary=dictionary)
            if {item['code'] for item in issues} & TREND_BLOCKING_ISSUES == {'unit_unknown'}:
                issues = catalog_issues(project_catalog(effective), issues, effective)
            if not {item['code'] for item in issues} & TREND_BLOCKING_ISSUES:
                effective.capability_level = definition.capability_level.value
    return effective


def lock_observation(observation_id):
    identity = LabObservation.objects.filter(pk=observation_id).values_list(
        "parsing_version__document_id", flat=True,
    ).first()
    if identity is None:
        raise PermissionDenied
    from apps.documents.models import Document
    from apps.patients.models import Patient
    patient_id = Document.objects.filter(pk=identity).values_list("patient_id", flat=True).first()
    if not Patient.objects.select_for_update().filter(pk=patient_id, deleted_at__isnull=True).exists():
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
        if len(value) > limit or (name not in {"raw_unit", "physiological_phase", "reference_range_raw",
                                             "report_flag_raw", "specimen", "method_raw"} and not value):
            raise ValidationError("更正内容为空或过长。")
        output[name] = value
    if 'physiological_phase' in output:
        from .catalog import PHASES
        if output['physiological_phase'] and output['physiological_phase'] not in PHASES:
            raise ValidationError("请选择本次检测明确的生理阶段，或清空阶段。")
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
        kind = _result_type(output["raw_value"]) or ResultType.STATUS
        output["result_type"] = str(kind)
    if "standard_code" in output or "raw_name" in output:
        from .extraction import _candidate_identity
        from tools.sample_dictionary.normalize import normalize_candidate_name

        dictionary = current_dictionary()
        definition = None
        if "standard_code" in output:
            definition = next((item for item in dictionary.indicators if item.code == output["standard_code"]), None)
            if definition is None:
                raise ValidationError("请选择现有目录中的项目编码。")
        else:
            definition = dictionary.match(output["raw_name"], specimen=output.get('specimen', effective.specimen))
        if definition is None:
            name = normalize_candidate_name(output['raw_name'], strip_result=False)
            _, code, standard_name, capability = _candidate_identity(name, dictionary,
                specimen=output.get('specimen', effective.specimen))
            output.update(standard_code=code, standard_name=standard_name, capability_level=capability,
                          mapping_dictionary_version=dictionary.version)
            return output
        output.update(
            standard_code=definition.code, standard_name=definition.standard_name,
            capability_level=definition.capability_level.value,
            mapping_dictionary_version=dictionary.version,
        )
    return output


def append_revision(actor, observation, *, action, changes, expected_revision, origin="USER", resolved_issues=(),
                    exclusion_reason="", resolution_keep=None):
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
    if action == RevisionAction.EXCLUDE and exclusion_reason not in {'MISRECOGNIZED', 'DUPLICATE'}:
        raise ValidationError('请选择误识别或重复识别原因。')
    if action != RevisionAction.EXCLUDE and exclusion_reason:
        raise ValidationError('当前操作不能填写排除原因。')
    if (action == RevisionAction.RECONCILE and not isinstance(resolution_keep, bool)
            or action != RevisionAction.RECONCILE and resolution_keep is not None):
        raise ValidationError('人工条目冲突处理内容无效。')
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
        elif action == RevisionAction.EXCLUDE:
            if effective.excluded:
                raise ValidationError('此项已经排除。')
            after.update(excluded=True, exclusion_reason=exclusion_reason)
        elif action == RevisionAction.RESTORE:
            if not effective.excluded:
                raise ValidationError('此项未被排除。')
            if effective.exclusion_reason == 'RECONCILED':
                raise ValidationError('此项属于人工条目冲突，请重新选择唯一保留项目。')
            after.update(excluded=False, exclusion_reason='')
        elif action == RevisionAction.RECONCILE:
            if not effective.manual_conflict:
                raise ValidationError('当前没有待处理的人工条目冲突。')
            after.update(manual_conflict='', excluded=not resolution_keep,
                         exclusion_reason='' if resolution_keep else 'RECONCILED')
        elif action == RevisionAction.CORRECT:
            checked = _checked_changes(effective, changes)
            after.update(checked)
            for name in set(checked) & set(VALUE_FIELDS):
                after["value_sources"][name] = _source_identity(observation, event_id)
            # A field edit cannot acknowledge unrelated source/reparse differences.
            if set(changes) != {'physiological_phase'}:
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
    from apps.operations.audit import record_audit_event
    record_audit_event(actor.pk, "lab_revised", observation.pk, "succeeded", action.lower(),
                       patient_id=observation.parsing_version.document.patient_id)
    return event


def revise_observation(actor, observation_id, *, action, changes, expected_revision):
    with transaction.atomic():
        document, observation = lock_observation(observation_id)
        actor = authorize_patient(document.patient, actor, "write").actor
        return append_revision(actor, observation, action=action, changes=changes, expected_revision=expected_revision)
