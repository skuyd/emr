"""Patient-serialized collection, review and explicit display choices."""

from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.documents.models import Document
from apps.operations.audit import record_audit_event
from apps.patients.access import authorize_patient, Capability

from . import matching
from .models import CancerCandidate, CandidateRevision, CollectionRun, CollectionCandidate, DisplaySelection, SelectionRevision
from .profiles import PROFILE_VERSION, PROFILES
from .readmodels import candidate_state, collection_current, history_authors, occurrence_key, resolve_ordering
from .schema import Assertion, Subject, SelectionMode
from .sources import SourceContext, author_state


class OrderingConflict(ValueError):
    pass


def _expected(actual, expected):
    if type(expected) is not int or expected != actual:
        raise OrderingConflict('记录已更新，请刷新后重新核对。')


def _lock_documents(patient):
    for identity in Document.objects.filter(patient=patient, deleted_at__isnull=True).order_by('pk').values_list('pk', flat=True):
        lock_document_aggregate(identity, patient_id=patient.pk)


def _collection(scope, patient, author, *, status, count=0, reason=''):
    previous = CollectionRun.objects.filter(patient=patient, scope_key=scope.key).order_by('-sequence').first()
    run = CollectionRun(patient=patient, document=scope.document, parsing_version=scope.parsing_version,
        source_fact=scope.source_fact, scope_key=scope.key, sequence=previous.sequence + 1 if previous else 1,
        rule_version=matching.MATCHING_VERSION, input_fingerprint=scope.input_fingerprint, input_snapshot=scope.input_snapshot,
        status=status, error_code=reason, candidate_count=count, author_id=author, author_snapshot=author_state(author))
    run.full_clean()
    run.save()
    return run


def collect_scope(scope, *, patient, author):
    """Caller holds the patient/document guards; failures use a separate savepoint.

    The worker supplies a scope captured under its current processing lease.
    No exception text, provider payload or source excerpt becomes an error code.
    """
    previous = CollectionRun.objects.filter(patient=patient, scope_key=scope.key).order_by('-sequence').first()
    if collection_current(previous, scope):
        return previous
    if not scope.complete:
        return _collection(scope, patient, author, status='FAILED', reason=scope.reason)
    try:
        with transaction.atomic():
            candidates = []
            for source in scope.facts:
                for data in matching.literal_candidates(source.text, source.category):
                    identity = occurrence_key(source, data)
                    candidate = CancerCandidate.objects.filter(source_fact=source.fact, occurrence_key=identity).first()
                    if candidate is None:
                        binding = source.candidate_binding(data)
                        candidate = CancerCandidate(patient=patient, document=scope.document, source_fact=source.fact,
                            source_report=source.fact.clinical_report, occurrence_key=identity,
                            rule_version=matching.MATCHING_VERSION, original_data=data, original_source=binding, created_by_id=author)
                        # The empty history and actual collector are captured as
                        # source authors, not as a clinical confirmation.
                        from apps.facts.readmodels import digest
                        candidate.original_source['author_fingerprint'] = digest({'creator': author_state(author), 'revisions': []})
                        candidate.full_clean()
                        candidate.save()
                    candidates.append(candidate)
            run = _collection(scope, patient, author, status='COMPLETE', count=len(candidates))
            for ordinal, candidate in enumerate(candidates, start=1):
                member = CollectionCandidate(collection=run, candidate=candidate, ordinal=ordinal)
                member.full_clean()
                member.save()
            return run
    except Exception:
        # The nested transaction rolled back all partial candidates and links.
        # Persist the failed attempt while keeping the source archive available.
        return _collection(scope, patient, author, status='FAILED', reason='candidate_collection_failed')


def collect_current(patient, *, actor):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        _lock_documents(access.patient)
        scopes = SourceContext().scopes(access.patient)
        runs = tuple(collect_scope(scope, patient=access.patient, author=access.actor.pk) for scope in scopes)
        failed = any(run.status == 'FAILED' for run in runs)
        record_audit_event(access.actor.pk, 'cancer_collection_requested', access.patient.pk,
                           'failed' if failed else 'succeeded', 'collection_failed' if failed else None)
        return runs


def _correct(content, changes):
    if not isinstance(changes, dict) or set(changes) != {'label', 'profile', 'assertion', 'subject'}:
        raise ValidationError('更正须明确填写表述、显示组、断言及所属对象；原文区间不可改写。')
    label = changes['label']
    if (not isinstance(label, str) or not label.strip() or len(label) > 160
            or changes['assertion'] not in {item.value for item in Assertion}
            or changes['subject'] not in {item.value for item in Subject}
            or matching.ALIASES.get(label.strip()) != changes['profile']
            or changes['profile'] not in {None, 'LUNG', 'PANCREAS'}):
        raise ValidationError('请使用已有完整表述及其对应显示组；未支持表述保持未知。')
    result = deepcopy(content)
    result.update(changes)
    result['label'] = label.strip()
    return result


def revise_candidate(patient, candidate_id, *, actor, action, expected_revision, expected_source,
                     checked_original=False, changes=None, reason=''):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
        identity = CancerCandidate.objects.filter(pk=candidate_id, patient=access.patient).values_list('document_id', flat=True).first()
        if identity is None:
            raise PermissionDenied('候选不属于当前患者。')
        document, _ = lock_document_aggregate(identity, patient_id=access.patient.pk)
        if document is None or document.deleted_at is not None:
            raise PermissionDenied('原件不可用。')
        candidate = CancerCandidate.objects.select_for_update().get(pk=candidate_id, patient=access.patient)
        _expected(candidate.revision_number, expected_revision)
        row = candidate_state(candidate)
        if not expected_source or expected_source != row['current_source_token']:
            raise OrderingConflict('来源或核对记录已变化，请重新打开原件。')
        if action not in {'CONFIRM', 'CORRECT', 'EXCLUDE', 'DEFER', 'REVOKE', 'UNDO'}:
            raise ValidationError('未知核对操作。')
        if changes and action != 'CORRECT':
            raise ValidationError('请使用更正操作修改候选内容。')
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValidationError('核对说明过长。')
        if action in {'CONFIRM', 'CORRECT'}:
            if checked_original is not True:
                raise ValidationError('请先对照原件核对；此操作不判断报告的医学结论。')
            if not row['source_valid']:
                raise OrderingConflict('当前摘录或解析来源不可用，请先核对父来源并重新收集。')
        before = deepcopy(row['recorded_state'])
        before['status'] = row['status']
        before['requires_review'] = row['source_changed'] or before.get('requires_review', False)
        after = deepcopy(before)
        if action == 'UNDO':
            latest = candidate.revisions.order_by('-sequence').first()
            if latest is None or latest.action == 'UNDO':
                raise ValidationError('没有可撤销的上一项操作。')
            after = deepcopy(latest.before)
            source_matches = (after['input_fingerprint'] == row['source_input_fingerprint'] if after['basis'] == 'ORIGINAL'
                              else after['source_token'] == row['parent_source_token'])
            authors_match = after['author_fingerprint'] == history_authors(candidate, through=after.get('author_sequence', 0))
            if not source_matches or not authors_match:
                after['requires_review'] = True
                if after['status'] == 'CONFIRMED':
                    after['status'] = 'PENDING'
        else:
            after.update(status={'CONFIRM': 'CONFIRMED', 'CORRECT': 'CONFIRMED', 'EXCLUDE': 'EXCLUDED',
                                  'DEFER': 'DEFERRED', 'REVOKE': 'PENDING'}[action],
                         basis='REVIEW', input_fingerprint=row['source_input_fingerprint'], source_token=row['parent_source_token'],
                         requires_review=action not in {'CONFIRM', 'CORRECT'} and before['requires_review'])
        if action == 'CORRECT':
            if not reason.strip():
                raise ValidationError('请说明本次人工更正的依据。')
            after['content'] = _correct(before['content'], changes)
            after['manual_correction'] = True
        after['author_fingerprint'] = history_authors(candidate, append=access.actor.pk)
        after['author_sequence'] = candidate.revision_number + 1
        revision = CandidateRevision.objects.create(candidate=candidate, author=access.actor, sequence=candidate.revision_number + 1,
            action=action, before=before, after=after, checked_original=checked_original, reason=reason.strip(),
            source_token=row['parent_source_token'])
        candidate.revision_number += 1
        candidate.save(update_fields=['revision_number'])
        record_audit_event(access.actor.pk, 'cancer_candidate_revised', candidate.pk, 'succeeded', action.lower())
        return revision


def _selection_access(patient, actor, expected_revision, expected_fingerprint):
    access = authorize_patient(patient, actor, Capability.WRITE, lock=True)
    _lock_documents(access.patient)
    state = resolve_ordering(access.patient)
    _expected(state['revision_number'], expected_revision)
    if not expected_fingerprint or expected_fingerprint != state['fingerprint']:
        raise OrderingConflict('诊断来源、候选或显示选择已变化，请刷新。')
    return access, state


def _selection_event(access, state, after, action):
    selection, _ = DisplaySelection.objects.get_or_create(patient=access.patient)
    after['author_fingerprint'] = history_authors(selection, append=access.actor.pk)
    candidate_id = after.get('candidate_id')
    candidate = CancerCandidate.objects.filter(pk=candidate_id, patient=access.patient).first() if candidate_id else None
    event = SelectionRevision.objects.create(selection=selection, candidate=candidate, author=access.actor,
        sequence=selection.revision_number + 1, action=action, before=state['selection_state'], after=after)
    selection.revision_number += 1
    selection.save(update_fields=['revision_number'])
    record_audit_event(access.actor.pk, 'cancer_display_selected', access.patient.pk, 'succeeded', action.lower())
    return event


def select_ordering(patient, *, actor, mode, expected_revision, expected_fingerprint, profile='', candidate_id=None):
    with transaction.atomic():
        access, state = _selection_access(patient, actor, expected_revision, expected_fingerprint)
        if mode not in {item.value for item in SelectionMode}:
            raise ValidationError('请选择有效显示模式。')
        if ((mode == 'MANUAL_PROFILE' and (profile not in PROFILES or profile == 'GENERAL' or candidate_id))
                or (mode in {'AUTO', 'GENERAL'} and (profile or candidate_id))
                or (mode == 'CANDIDATE' and (profile or not candidate_id))):
            raise ValidationError('显示模式与选择的依据不匹配。')
        after = {'mode': mode, 'profile': profile, 'candidate_id': None, 'candidate_fingerprint': '',
                 'profile_version': PROFILE_VERSION, 'author_fingerprint': ''}
        if mode == 'CANDIDATE':
            chosen = next((row for row in state['candidates'] if row['id'] == str(candidate_id)), None)
            if chosen is None:
                raise PermissionDenied('候选不属于当前患者。')
            if (not chosen['source_valid'] or chosen['source_changed'] or chosen['status'] in {'EXCLUDED', 'DEFERRED'}
                    or not matching.eligible_for_auto(chosen['content'], (), source_valid=True, reviewed=True)):
                raise ValidationError('请先核对当前明确表述，或直接选择手工显示偏好。')
            after.update(candidate_id=chosen['id'], candidate_fingerprint=chosen['fingerprint'])
        return _selection_event(access, state, after, 'SELECT')


def undo_selection(patient, *, actor, expected_revision, expected_fingerprint):
    with transaction.atomic():
        access, state = _selection_access(patient, actor, expected_revision, expected_fingerprint)
        selection = DisplaySelection.objects.filter(patient=access.patient).first()
        latest = selection.revisions.order_by('-sequence').first() if selection else None
        if latest is None or latest.action == 'UNDO':
            raise ValidationError('没有可撤销的上一项显示选择。')
        # Retain the previous candidate/source fingerprint. Undo cannot silently
        # rebind a stale choice to a different current source.
        return _selection_event(access, state, deepcopy(latest.before), 'UNDO')
