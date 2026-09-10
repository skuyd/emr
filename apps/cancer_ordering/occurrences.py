"""Preserve review barriers across real source types and source replacement."""
from copy import deepcopy

from apps.facts.readmodels import digest

from .models import OccurrenceReview
from .sources import author_state


def position_identity(version_id, fragments):
    from .narrative_sources import position_key
    pages = {str(item['page_id']) for item in fragments if item.get('page_id')}
    exact = bool(fragments) and all(item.get('block_id') and type(item.get('start')) is int for item in fragments)
    key = position_key(version_id, fragments) if exact else digest({
        'version_id': str(version_id) if version_id else None, 'unlocated_pages': sorted(pages)})
    return key, pages, exact


def original_position(candidate):
    source = candidate.source_narrative if candidate.source_narrative_id else candidate.source_fact
    if source is None:
        return None, None
    return source.parsing_version_id, position_identity(source.parsing_version_id,
        candidate.original_source.get('label_fragments', []))[0]


def retain_review(candidate, revision):
    version_id, key = original_position(candidate)
    if key is None:
        return
    OccurrenceReview.objects.get_or_create(original_revision_id=revision.pk, defaults={
        'document_id': candidate.document_id, 'parsing_version_id': version_id, 'position_key': key,
        'candidate': candidate, 'original_candidate_id': candidate.pk,
        'author_id': revision.author_id, 'action': revision.action, 'before': deepcopy(revision.before),
        'after': deepcopy(revision.after), 'original_source': deepcopy(candidate.original_source),
        'reviewed_at': revision.created_at})


def review_barrier(candidate):
    version_id, key = original_position(candidate)
    if key is None:
        return {'fingerprint': '', 'requires_review': False, 'status': None}
    _, pages, exact = position_identity(version_id, candidate.original_source.get('label_fragments', []))
    events = []
    for event in OccurrenceReview.objects.filter(document_id=candidate.document_id,
            parsing_version_id=version_id).order_by('reviewed_at', 'original_revision_id'):
        event_key, event_pages, event_exact = position_identity(version_id, event.original_source.get('label_fragments', []))
        possible_overlap = (not exact or not event_exact) and (not pages or not event_pages or bool(pages & event_pages))
        if event_key == key or possible_overlap:
            events.append(event)
    latest_own = next((index for index in range(len(events) - 1, -1, -1)
                       if events[index].original_candidate_id == candidate.pk), -1)
    later_other = [event for event in events[latest_own + 1:] if event.original_candidate_id != candidate.pk]
    barrier = later_other[-1] if later_other else None
    return {'requires_review': barrier is not None,
        'status': barrier.after.get('status') if barrier and barrier.after.get('status') in {'EXCLUDED', 'DEFERRED'} else None,
        'fingerprint': digest([{'id': str(event.pk), 'candidate': str(event.original_candidate_id),
            'revision': str(event.original_revision_id), 'action': event.action,
            'before': event.before, 'after': event.after, 'author': author_state(event.author_id),
            'reviewed_at': event.reviewed_at} for event in events])}
