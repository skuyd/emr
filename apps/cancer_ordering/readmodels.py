"""Current report candidates and the complete display-order dependency set."""

from copy import deepcopy

from apps.facts.readmodels import digest, source_info

from . import matching
from .models import CancerCandidate, CollectionRun, DisplaySelection
from .profiles import PROFILE_VERSION, PROFILES
from .sources import SOURCE_VERSION, SourceContext, author_state


def occurrence_key(source, content):
    return digest({'fact_id': str(source.fact.pk), 'category': source.category, 'content': content})


def history_authors(aggregate, *, append=None, through=None):
    revisions = aggregate.revisions.order_by('sequence')
    if through is not None:
        revisions = revisions.filter(sequence__lte=through)
    values = [[row.sequence, author_state(row.author_id)] for row in revisions]
    if append is not None:
        values.append([aggregate.revision_number + 1, author_state(append)])
    return digest({'creator': author_state(aggregate.created_by_id) if isinstance(aggregate, CancerCandidate) else None,
                   'revisions': values})


def candidate_state(candidate, *, context=None):
    context = context or SourceContext()
    source = context.fact(candidate.source_fact_id)
    latest = candidate.revisions.order_by('-sequence').first()
    authors = history_authors(candidate)
    state = deepcopy(latest.after) if latest else {
        'content': deepcopy(candidate.original_data), 'status': 'PENDING', 'basis': 'ORIGINAL',
        'input_fingerprint': candidate.original_source['input_fingerprint'],
        'source_token': candidate.original_source['source_token'],
        'author_fingerprint': candidate.original_source['author_fingerprint'], 'author_sequence': 0,
        'manual_correction': False, 'requires_review': False,
    }
    present = (candidate.rule_version == matching.MATCHING_VERSION and any(
        occurrence_key(source, row) == candidate.occurrence_key
        for row in matching.literal_candidates(source.text, source.category)))
    valid = source.source_valid and present
    identity_changed = (state['author_fingerprint'] != authors or (
        state['input_fingerprint'] != source.input_fingerprint if state['basis'] == 'ORIGINAL'
        else state['source_token'] != source.source_token))
    changed = identity_changed or state.get('requires_review', False)
    status = 'PENDING' if identity_changed or not valid or (state['status'] == 'CONFIRMED' and changed) else state['status']
    token = digest({'parent': source.source_token, 'candidate': str(candidate.pk),
                    'head': candidate.revision_number, 'authors': authors,
                    'occurrence': candidate.occurrence_key, 'rule': matching.MATCHING_VERSION})
    row = {'id': str(candidate.pk), 'document_id': str(candidate.document_id),
           'revision_number': candidate.revision_number, 'content': deepcopy(state['content']),
           'original_data': deepcopy(candidate.original_data), 'status': status, 'recorded_status': state['status'],
           'source_valid': bool(valid), 'source_changed': bool(changed), 'source_present': present,
           'current_source_token': token, 'manual_correction': state['manual_correction'],
           'source': source_info(source.fact), 'binding_kind': source.binding_kind,
           'parent_status': source.status, 'parent_source_token': source.source_token,
           'source_input_fingerprint': source.input_fingerprint,
           'source_confidence': source.confidence_values, 'recorded_state': state,
           'reason': 'source_unavailable' if not valid else 'original_review_required' if changed else ''}
    # Original OCR proves the original candidate only. A retained manual
    # correction needs a current confirmation, including after REVOKE/UNDO.
    automatic_confidence = () if row['manual_correction'] else source.confidence_values
    row['eligible_for_auto'] = matching.eligible_for_auto(row['content'], automatic_confidence,
        source_valid=valid and not changed and status not in {'EXCLUDED', 'DEFERRED'}, reviewed=status == 'CONFIRMED')
    row['fingerprint'] = digest({'id': row['id'], 'original': candidate.original_data,
        'original_source': candidate.original_source, 'state': state, 'current_source': token,
        'valid': valid, 'changed': changed, 'status': status, 'authors': authors})
    return row


def candidate_rows(patient, *, context=None):
    context = context or SourceContext()
    return tuple(candidate_state(candidate, context=context) for candidate in CancerCandidate.objects.filter(
        patient_id=getattr(patient, 'pk', patient)).order_by('created_at', 'pk'))


def collection_current(run, scope):
    return bool(run and scope.complete and run.status == 'COMPLETE'
                and run.input_fingerprint == scope.input_fingerprint and run.rule_version == matching.MATCHING_VERSION
                and run.author_snapshot == author_state(run.author_id)
                and run.candidate_count == run.members.count())


def selection_state(patient):
    selection = DisplaySelection.objects.filter(patient_id=getattr(patient, 'pk', patient)).first()
    latest = selection.revisions.order_by('-sequence').first() if selection else None
    default = {'mode': 'AUTO', 'profile': '', 'candidate_id': None, 'candidate_fingerprint': '',
               'profile_version': PROFILE_VERSION, 'author_fingerprint': ''}
    state = deepcopy(latest.after) if latest else default
    authors = history_authors(selection) if selection else ''
    valid = (not latest or (state['author_fingerprint'] == authors and author_state(latest.author_id)['active']))
    valid = valid and state['profile_version'] == PROFILE_VERSION
    return selection, state, valid, authors


def _auto(rows, complete):
    if not complete:
        return 'GENERAL', 'collection_incomplete'
    considered = [row for row in rows if row['status'] not in {'EXCLUDED', 'DEFERRED'}
                  and row['parent_status'] not in {'EXCLUDED', 'DEFERRED'}
                  and row['content']['assertion'] != 'NEGATED'
                  and row['content']['subject'] not in {'HISTORICAL', 'OTHER_PERSON', 'METASTATIC_SITE'}]
    if not considered:
        return 'GENERAL', 'no_reported_diagnosis'
    profiles = {row['content']['profile'] for row in considered}
    if len(profiles) > 1:
        return 'GENERAL', 'reported_diagnoses_differ'
    if None in profiles:
        return 'GENERAL', 'unsupported_reported_diagnosis'
    if not all(row['eligible_for_auto'] for row in considered):
        return 'GENERAL', 'original_review_required'
    return profiles.pop(), 'reported_diagnosis'


def resolve_ordering(patient):
    context = SourceContext()
    scopes = context.scopes(patient)
    rows = candidate_rows(patient, context=context)
    current_ids, scope_states, complete = set(), [], True
    for scope in scopes:
        run = CollectionRun.objects.filter(patient_id=getattr(patient, 'pk', patient), scope_key=scope.key).order_by('-sequence').first()
        members = list(run.members.order_by('ordinal').values_list('candidate_id', flat=True)) if run else []
        valid = collection_current(run, scope)
        complete = complete and valid
        if valid:
            current_ids.update(str(value) for value in members)
        scope_states.append({'key': scope.key, 'input': scope.input_fingerprint, 'live': scope.live_fingerprint,
            'complete': valid, 'run': {'id': str(run.pk), 'sequence': run.sequence, 'input': run.input_fingerprint,
                'rule': run.rule_version, 'status': run.status, 'count': run.candidate_count,
                'author': author_state(run.author_id), 'members': [str(value) for value in members]} if run else None})
    selection, selected, selection_valid, selection_authors = selection_state(patient)
    mode = selected['mode']
    profile, reason = 'GENERAL', 'selection_source_changed'
    if not selection_valid:
        reason = 'selection_author_or_configuration_changed'
    elif mode == 'AUTO':
        profile, reason = _auto([row for row in rows if row['id'] in current_ids], complete)
    elif mode == 'GENERAL':
        reason = 'explicit_general'
    elif mode == 'MANUAL_PROFILE' and selected['profile'] in PROFILES:
        profile, reason = selected['profile'], 'manual_display_preference'
    elif mode == 'CANDIDATE':
        chosen = next((row for row in rows if row['id'] == selected['candidate_id']), None)
        if (chosen and chosen['fingerprint'] == selected['candidate_fingerprint']
                and chosen['source_valid'] and not chosen['source_changed']
                and chosen['status'] not in {'EXCLUDED', 'DEFERRED'}
                and matching.eligible_for_auto(chosen['content'], (), source_valid=True, reviewed=True)):
            profile, reason = chosen['content']['profile'], 'selected_reported_diagnosis'
    fingerprint = digest({'patient': str(getattr(patient, 'pk', patient)), 'source_rule': SOURCE_VERSION,
        'matching_rule': matching.MATCHING_VERSION, 'profile_version': PROFILE_VERSION,
        'scopes': scope_states, 'candidates': [row['fingerprint'] for row in rows],
        'selection': selected, 'selection_authors': selection_authors,
        'selection_head': selection.revision_number if selection else 0,
        'effective_profile': profile, 'reason': reason})
    return {'mode': mode, 'profile': profile, 'reason': reason, 'fingerprint': fingerprint,
            'revision_number': selection.revision_number if selection else 0, 'candidates': rows,
            'complete': complete, 'scope_states': tuple(scope_states), 'selection_state': selected}
