"""Review source-authorized candidates, then publish immutable, regression-checked releases."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.models import Document, UploadBatch
from apps.operations.audit import record_audit_event
from apps.operations.models import DictionaryEvaluationEvent, DictionaryPublicationLock, DictionaryRelease
from apps.operations.permissions import Action, authorize, current_actor
from apps.patients.models import Patient

from .dictionary import (
    DictionaryError, current_dictionary, dictionary_for_release, load_dictionary_content, normalize_indicator_alias,
    release_digest, rules_digest, rules_for_version,
)
from .extraction import _unit_key
from .models import DictionaryCandidate, DictionaryCandidateEvent, DictionaryCandidateSource, ReviewTaskStatus
from .review import _is_reviewer
from .revisions import RevisionConflict


class DictionaryWorkflowError(ValueError):
    pass


def _encoded(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload(dictionary):
    release = DictionaryRelease.objects.filter(version=dictionary.version, content_hash=dictionary.content_hash).first()
    if release is not None and release.payload:
        return deepcopy(release.payload)
    return json.loads(dictionary.source_path.read_text(encoding="utf-8"))


def candidate_sources(actor, candidate):
    sources = candidate.sources.filter(
        observation__parsing_version__document__deleted_at__isnull=True,
        observation__parsing_version__document__patient__account__is_active=True,
        observation__parsing_version__active=True,
    ).select_related("observation__evidence", "observation__document_page", "observation__parsing_version__document")
    if not actor.is_active:
        return sources.none()
    if candidate.patient.account_id == actor.pk:
        return sources
    if not _is_reviewer(actor):
        return sources.none()
    # All grant conditions must apply to the same task, not different related rows.
    return sources.filter(
        observation__review_tasks__reviewer_id=actor.pk,
        observation__review_tasks__revoked_at__isnull=True,
        observation__review_tasks__expires_at__gt=timezone.now(),
        observation__review_tasks__status__in=[ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS, ReviewTaskStatus.COMPLETED, ReviewTaskStatus.UNABLE],
    ).distinct()


def get_dictionary_candidate(actor, candidate_id):
    candidate = DictionaryCandidate.objects.select_related("patient").filter(pk=candidate_id).first()
    if candidate is None or not candidate_sources(actor, candidate).exists():
        raise PermissionDenied
    return candidate


def collect_dictionary_candidates(version):
    if version.document.deleted_at is not None:
        return ()
    from .dictionary import dictionary_for_version

    dictionary = dictionary_for_version(version.dictionary_version)
    definitions = {item.code: item for item in dictionary.indicators}
    collected = {}
    for observation in version.lab_observations.order_by("reading_order", "pk"):
        definition = definitions.get(observation.standard_code)
        entries = []
        if definition is None:
            entries.append(("PROJECT", observation.raw_name, ""))
        elif observation.raw_unit and _unit_key(observation.raw_unit) not in {_unit_key(unit) for unit in definition.unit_forms}:
            entries.append(("UNIT", observation.raw_unit, observation.standard_code))
        for kind, raw_term, code in entries:
            key = hashlib.sha256(f"{kind}:{normalize_indicator_alias(raw_term)}:{code}:{observation.specimen}".encode("utf-8")).hexdigest()
            candidate, _created = DictionaryCandidate.objects.get_or_create(
                patient_id=version.document.patient_id, normalized_key=key,
                defaults={"kind": kind, "raw_term": raw_term, "standard_code": code},
            )
            DictionaryCandidateSource.objects.get_or_create(candidate=candidate, observation=observation)
            collected[candidate.pk] = candidate
    return tuple(collected.values())


def _lock_candidates(candidate_ids):
    ids = tuple(dict.fromkeys(candidate_ids))
    patients = DictionaryCandidate.objects.filter(pk__in=ids).values_list("patient_id", flat=True)
    list(Patient.objects.select_for_update().filter(pk__in=patients).order_by("pk"))
    documents = Document.objects.filter(parsing_versions__lab_observations__dictionary_sources__candidate_id__in=ids).distinct()
    list(UploadBatch.objects.select_for_update().filter(pk__in=documents.values("batch_id")).order_by("pk"))
    list(Document.objects.select_for_update().filter(pk__in=documents.values("pk")).order_by("pk"))
    candidates = tuple(DictionaryCandidate.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(candidates) != len(ids):
        raise PermissionDenied
    return candidates


def _compose(base, definitions, *, version=None):
    payload = _payload(base)
    by_code = {item["code"]: item for item in payload["indicators"]}
    for definition in definitions:
        if not isinstance(definition, dict) or not isinstance(definition.get("code"), str):
            raise DictionaryWorkflowError("字典定义不完整。")
        by_code[definition["code"]] = deepcopy(definition)
    payload["indicators"] = list(by_code.values())
    if version is not None:
        payload["dictionary_version"] = version
    try:
        dictionary = load_dictionary_content(_encoded(payload))
    except DictionaryError as error:
        raise DictionaryWorkflowError("字典定义或别名存在冲突。") from error
    return payload, dictionary


def _checked_rules(actor, rules, definition, rationale, dictionary):
    from .validation import numeric_value

    if not isinstance(rules, list):
        raise ValidationError("规则必须是列表。")
    checked = []
    ids = set()
    definitions = {item.code: item for item in dictionary.indicators}
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("code") != definition["code"] or not all(
            isinstance(rule.get(name), str) and rule[name].strip()
            for name in ("id", "version", "kind", "specimen", "method", "evidence")
        ):
            raise ValidationError("规则须说明项目、标本、方法、版本和依据。")
        kind = rule["kind"]
        if rule['id'] in ids:
            raise ValidationError("同一审核中规则标识不能重复。")
        ids.add(rule['id'])
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", rule['id'])
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", rule['version'])
                or rule['specimen'] not in {'BLOOD', 'URINE', 'STOOL', 'OTHER'}
                or definition.get('specimen') and definition['specimen'] != rule['specimen']):
            raise ValidationError("规则的标识、版本或标本与项目不符。")
        required = {"conversion": ("source_unit", "target_unit", "factor"),
                    "history_ratio": ("unit", "minimum_ratio"),
                    "report_sum": ("unit", "absolute_tolerance", "component_codes")}.get(kind)
        if required is None or any(name not in rule for name in required):
            raise ValidationError("规则类型或前置条件不完整。")
        unit_fields = ('source_unit', 'target_unit') if kind == 'conversion' else ('unit',)
        known_units = {_unit_key(unit) for unit in definition['unit_forms']}
        if any(not isinstance(rule.get(name), str) or not rule[name].strip() or len(rule[name]) > 64
               or _unit_key(rule[name]) not in known_units for name in unit_fields):
            raise ValidationError("规则单位必须已在该项目定义中核实。")
        if kind == 'report_sum':
            components = rule['component_codes']
            if (not isinstance(components, list) or not components or any(not isinstance(code, str) for code in components)
                    or len(components) != len(set(components)) or definition['code'] in components
                    or any(code not in definitions or _unit_key(rule['unit']) not in {
                        _unit_key(unit) for unit in definitions[code].unit_forms
                    } for code in components)):
                raise ValidationError("报告关系规则须列出已定义且单位一致的不同组成项目。")
        number_field = {"conversion": "factor", "history_ratio": "minimum_ratio", "report_sum": "absolute_tolerance"}[kind]
        value = numeric_value(rule[number_field])
        if value is None or value < 0 or (kind == "conversion" and value == 0) or (kind == "history_ratio" and value <= 1):
            raise ValidationError("规则数值无效。")
        checked.append({**deepcopy(rule), "reviewed_by": str(actor.pk), "rationale": rationale})
    return checked


def review_candidate(actor, candidate_id, *, decision, definition, rationale, expected_revision, rules=None, totp_verified_at=None):
    authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
    if decision not in {"ACCEPT", "REJECT"} or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
        raise ValidationError("请填写审核结论及依据。")
    with transaction.atomic():
        candidate = _lock_candidates([candidate_id])[0]
        actor = current_actor(actor)
        authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
        get_dictionary_candidate(actor, candidate.pk)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or candidate.revision_number != expected_revision:
            raise RevisionConflict("候选已经更新。")
        checked_rules = []
        if decision == "ACCEPT":
            _payload_value, proposed = _compose(current_dictionary(), [definition])
            sources = tuple(candidate_sources(actor, candidate))
            source_specimens = {item.observation.specimen for item in sources if item.observation.specimen}
            if definition.get('specimen') and source_specimens and source_specimens != {definition['specimen']}:
                raise DictionaryWorkflowError("项目标本必须与本候选已授权的原始来源一致。")
            if candidate.kind == 'UNIT':
                if candidate.standard_code != definition['code'] or _unit_key(candidate.raw_term) not in {
                    _unit_key(unit) for unit in definition['unit_forms']
                }:
                    raise DictionaryWorkflowError("单位候选必须绑定原项目并明确收录该单位。")
            else:
                # Context is evidence supplied by the authorized observation, never an assertion
                # supplied by the very definition under review.
                contextual_dictionary = any(item.specimen for item in proposed.indicators)
                matches = [proposed.match(candidate.raw_term, specimen=item.observation.specimen if contextual_dictionary else '')
                           for item in sources]
                if not matches or any(match is None or match.code != definition['code'] for match in matches):
                    raise DictionaryWorkflowError("审核定义必须明确匹配本候选的原始项目名称。")
            checked_rules = _checked_rules(actor, rules if rules is not None else [], definition, rationale.strip(), proposed)
            if source_specimens and any(source_specimens != {rule['specimen']} for rule in checked_rules):
                raise DictionaryWorkflowError("规则标本必须与本候选已授权的原始来源一致。")
        candidate.status = "ACCEPTED" if decision == "ACCEPT" else "REJECTED"
        candidate.definition = deepcopy(definition) if decision == "ACCEPT" else {}
        candidate.rules = checked_rules
        candidate.reviewed_by = actor
        candidate.reviewed_at = timezone.now()
        candidate.rationale = rationale.strip()
        candidate.revision_number += 1
        candidate.save()
        DictionaryCandidateEvent.objects.create(candidate=candidate, author=actor, sequence=candidate.revision_number,
                                                decision=decision, definition=candidate.definition, rules=checked_rules, rationale=candidate.rationale)
        return candidate


def evaluate_publication(dictionary, rules=(), *, baseline=None, baseline_rules=()):
    from .release_evaluation import evaluate_release_snapshot
    frozen = json.loads((Path(__file__).parent / 'dictionaries/phase-two-baseline.json').read_text(encoding='utf-8'))
    return evaluate_release_snapshot(dictionary, rules, baseline=baseline, baseline_rules=baseline_rules, baseline_report=frozen)


def deterministic_report(value):
    """Execution duration is retained as evidence but cannot stale an unchanged preview."""
    if isinstance(value, dict):
        return {key: deterministic_report(item) for key, item in value.items() if key != 'execution'}
    if isinstance(value, (list, tuple)):
        return [deterministic_report(item) for item in value]
    return value


def _regression_report(base, dictionary, rules=(), baseline_rules=()):

    failures = []
    checks = 0
    definitions = {item.code: item for item in dictionary.indicators}
    for before in base.indicators:
        after = definitions.get(before.code)
        if after is None or after.standard_name != before.standard_name:
            failures.append({"code": before.code, "reason": "stable_identity_changed"})
            continue
        for alias in (before.standard_name, *before.aliases, *before.ocr_variants):
            checks += 1
            matched = dictionary.match(alias, specimen=before.specimen, panel=before.category if before.specimen else "")
            if matched is None or matched.code != before.code:
                failures.append({"code": before.code, "reason": "known_alias_regressed"})
    fixed = evaluate_publication(dictionary, rules, baseline=base, baseline_rules=baseline_rules)
    return {"passed": not failures and fixed['passed'], "scope": "fixed_alias_and_synthetic_parser_regression", "checks": checks,
            "failures": failures, "fixed_synthetic": fixed,
            "baseline_hash": base.content_hash, "candidate_hash": dictionary.content_hash}


def _diff(base, dictionary):
    previous = {item.code: item for item in base.indicators}
    current = {item.code: item for item in dictionary.indicators}
    return {"added": sorted(current.keys() - previous.keys()), "removed": sorted(previous.keys() - current.keys()),
            "changed": sorted(code for code in current.keys() & previous.keys() if current[code] != previous[code])}


def preview_dictionary(actor, *, candidate_ids, version):
    base = current_dictionary()
    candidates = [get_dictionary_candidate(actor, identity) for identity in dict.fromkeys(candidate_ids)]
    if not candidates or any(candidate.status != "ACCEPTED" or candidate.reviewed_by_id is None for candidate in candidates):
        raise DictionaryWorkflowError("只能发布已经审核接受的候选。")
    definitions = {}
    for candidate in candidates:
        code = candidate.definition["code"]
        if code in definitions and definitions[code] != candidate.definition:
            raise DictionaryWorkflowError("同一项目有相互冲突的候选，请先合并审核。")
        definitions[code] = candidate.definition
    payload, dictionary = _compose(base, definitions.values(), version=version)
    previous_rules = {rule['id']: rule for rule in rules_for_version(base.version)}
    incoming_rules = {}
    for candidate in candidates:
        for rule in candidate.rules:
            previous = incoming_rules.get(rule['id'])
            if previous is not None and previous != rule:
                raise DictionaryWorkflowError("候选之间的同名规则冲突，请合并审核。")
            previous = previous_rules.get(rule['id'])
            if previous is not None and previous != rule and previous['version'] == rule['version']:
                raise DictionaryWorkflowError("规则内容变化必须使用新规则版本。")
            incoming_rules[rule['id']] = rule
    rules = list({**previous_rules, **incoming_rules}.values())
    report = _regression_report(base, dictionary, rules, list(previous_rules.values()))
    diff = _diff(base, dictionary)
    diff['rules'] = {'added': sorted(incoming_rules.keys() - previous_rules.keys()),
                     'changed': sorted(key for key in incoming_rules.keys() & previous_rules.keys() if incoming_rules[key] != previous_rules[key])}
    preview = {"payload": payload, "diff": _diff(base, dictionary), "regression_report": report,
               "expected_active_hash": base.content_hash, "candidate_revisions": {str(c.pk): c.revision_number for c in candidates},
               "rules": rules, "rules_hash": rules_digest(rules), "release_hash": release_digest(dictionary.content_hash, rules),
               "candidate_reviews": [{"candidate_id": str(c.pk), "revision": c.revision_number,
                   "reviewed_by": str(c.reviewed_by_id), "reviewed_at": c.reviewed_at.isoformat(),
                   "definition_hash": hashlib.sha256(_encoded(c.definition)).hexdigest(), "rules_hash": rules_digest(c.rules)} for c in candidates]}
    preview['diff'] = diff
    preview["preview_hash"] = hashlib.sha256(_encoded(deterministic_report(preview))).hexdigest()
    return preview


def _publication_lock():
    DictionaryPublicationLock.objects.get_or_create(key="dictionary")
    DictionaryPublicationLock.objects.select_for_update().get(key="dictionary")


def _baseline_release(base):
    release, _created = DictionaryRelease.objects.get_or_create(
        version=base.version, defaults={"content_hash": base.content_hash, "artifact_name": base.source_path.name,
                                      "indicator_count": len(base.indicators), "published_at": timezone.now(),
                                      "rules_hash": rules_digest([]), "release_hash": release_digest(base.content_hash, [])},
    )
    if release.content_hash != base.content_hash:
        raise DictionaryWorkflowError("已有版本的内容发生变化。")
    return release


def publish_dictionary(actor, *, version, candidate_ids, expected_active_hash, expected_preview_hash, totp_verified_at=None):
    authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version):
        raise ValidationError("版本标识无效。")
    with transaction.atomic():
        candidates = _lock_candidates(candidate_ids)
        _publication_lock()
        actor = current_actor(actor)
        authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
        base = current_dictionary()
        if base.content_hash != expected_active_hash:
            raise RevisionConflict("字典已经更新，请重新查看差异。")
        if DictionaryRelease.objects.filter(version=version).exists() or version == base.version:
            raise DictionaryWorkflowError("不能覆盖已有字典版本。")
        preview = preview_dictionary(actor, candidate_ids=[c.pk for c in candidates], version=version)
        if preview["preview_hash"] != expected_preview_hash:
            raise RevisionConflict("候选已经更新，请重新查看差异。")
        if not preview["regression_report"]["passed"]:
            raise DictionaryWorkflowError("固定回归未通过，不能发布。")
        # The fixed regression runs inside this transaction and can take time.
        actor = current_actor(actor)
        authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
        for candidate in candidates:
            get_dictionary_candidate(actor, candidate.pk)
        previous = _baseline_release(base)
        release = DictionaryRelease.objects.create(
            version=version, content_hash=preview["regression_report"]["candidate_hash"], artifact_name="",
            indicator_count=len(preview["payload"]["indicators"]), payload=preview["payload"],
            rules=preview['rules'], rules_hash=preview['rules_hash'], release_hash=preview['release_hash'],
            candidate_reviews=preview['candidate_reviews'], diff=preview["diff"], regression_report=preview["regression_report"],
            previous_release=previous, published_by=actor, published_at=timezone.now(),
        )
        DictionaryRelease.objects.filter(active=True).update(active=False)
        release.active = True
        release.save(update_fields=["active"])
        DictionaryEvaluationEvent.objects.create(release=release, actor=actor, action='PUBLISH', report=preview['regression_report'])
        record_audit_event(actor.pk, "dictionary_published", release.pk, "succeeded", "reviewed_candidates")
        return release


def rollback_dictionary(actor, release_id, *, expected_active_hash, totp_verified_at=None):
    authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
    with transaction.atomic():
        _publication_lock()
        if current_dictionary().content_hash != expected_active_hash:
            raise RevisionConflict("字典已经更新，请刷新后重新确认回退目标。")
        release = DictionaryRelease.objects.select_for_update().get(pk=release_id)
        dictionary = dictionary_for_release(release)
        report = evaluate_publication(dictionary, release.rules)
        if not report['passed']:
            raise DictionaryWorkflowError('回退目标未通过当前固定回归。')
        actor = current_actor(actor)
        authorize(actor, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
        DictionaryRelease.objects.filter(active=True).exclude(pk=release.pk).update(active=False)
        release.active = True
        release.save(update_fields=["active"])
        DictionaryEvaluationEvent.objects.create(release=release, actor=actor, action='ROLLBACK', report=report)
        record_audit_event(actor.pk, "dictionary_published", release.pk, "succeeded", "reviewed_rollback")
        return release


def remove_document_candidate_sources(document):
    candidates = list(DictionaryCandidate.objects.filter(sources__observation__parsing_version__document=document).values_list("pk", flat=True))
    DictionaryCandidateSource.objects.filter(observation__parsing_version__document=document).delete()
    DictionaryCandidate.objects.filter(pk__in=candidates, sources__isnull=True).delete()
