"""Exact share grants, never family membership, authorize shared cloud access."""
from django.core.exceptions import PermissionDenied
from django.views.decorators.debug import sensitive_variables

from apps.facts.readmodels import digest
from apps.patients.sharing import ShareUnavailable, authorize_share

from .readmodels import source_material, source_queryset
from .services import CloudConflict, _token
from .url_policy import validate_url


@sensitive_variables()
def shared_source(share_id, actor, key, source_id):
    access = authorize_share(share_id, actor, key)
    selected = access.share.scope.get('cloud_source_ids', [])
    if str(source_id) not in selected:
        raise PermissionDenied
    binding = access.share.cloud_sources.filter(source_identity=source_id).first()
    if binding is None or binding.source_id != binding.source_identity:
        authorize_share(share_id, actor, key)
        raise ShareUnavailable
    source = source_queryset().filter(pk=binding.source_id, patient=access.share.patient).first()
    if source is None:
        authorize_share(share_id, actor, key)
        raise ShareUnavailable
    material = source_material(source)
    if not material['usable'] or material['source_token'] != binding.source_token or material['revision_number'] != binding.revision_number:
        # The common validator commits scrubbing before propagating 410.
        authorize_share(share_id, actor, key)
        raise ShareUnavailable
    return access, material


@sensitive_variables()
def visit_shared_source(share_id, actor, key, source_id):
    access, row = shared_source(share_id, actor, key, source_id)
    return access, {'id': row['id'], 'site_label': validate_url(row['url']).site_label,
        'revision_number': row['revision_number'], 'source_token': row['source_token'],
        'read_token': digest({'share': str(access.share.pk), 'source': row['source_token'], 'snapshot': access.share.snapshot_digest})}


@sensitive_variables()
def open_shared_source(share_id, actor, key, source_id, *, expected_source, expected_revision):
    expected = _token(expected_source)
    access, row = shared_source(share_id, actor, key, source_id)
    if type(expected_revision) is not int or row['revision_number'] != expected_revision or row['source_token'] != expected:
        raise CloudConflict('来源已变化，请重新打开分享说明。')
    return access, validate_url(row['url'])
